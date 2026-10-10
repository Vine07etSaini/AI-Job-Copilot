"""Start (or find) a normal Chrome with a remote-debugging port, so Playwright can attach to it.

Chrome refuses remote debugging on its default profile, so it gets its own profile folder
(`storage/chrome-profile`). Log in to Naukri once in that window; the session lives there.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from job_copilot.config import STORAGE_DIR

log = logging.getLogger(__name__)

CHROME_PROFILE_DIR = STORAGE_DIR / "chrome-profile"
_CANDIDATES = [
    Path(os.environ.get(var, "")) / rel
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
    for rel in ("Google/Chrome/Application/chrome.exe",)
]


def find_chrome() -> str | None:
    for c in _CANDIDATES:
        if c.is_file():
            return str(c)
    return shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chromium")


def is_up(cdp_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=1.5):
            return True
    except OSError:
        return False


def ensure_chrome(cdp_url: str, wait_seconds: float = 20) -> bool:
    """Make sure a debuggable Chrome answers on `cdp_url`; start one if not. Returns True if we started it."""
    if is_up(cdp_url):
        return False
    exe = find_chrome()
    if exe is None:
        raise RuntimeError("Chrome not found. Install Google Chrome, or start it yourself with --remote-debugging-port.")
    port = urlparse(cdp_url).port or 9222
    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Starting Chrome on debug port %d (profile %s)", port, CHROME_PROFILE_DIR)
    # Detached: Chrome must outlive this process; we only ever disconnect from it.
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    subprocess.Popen(
        [exe, f"--remote-debugging-port={port}", f"--user-data-dir={CHROME_PROFILE_DIR}",
         "--no-first-run", "--no-default-browser-check",
         # Keep the page rendering (and screenshots working) when the window is covered, off-screen or the monitor is off.
         "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
         "--disable-background-timer-throttling", "--window-position=-32000,-32000",
         "https://www.naukri.com"],
        creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
    )
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if is_up(cdp_url):
            return True
        time.sleep(0.5)
    raise RuntimeError(f"Chrome did not open debug port {port} within {wait_seconds:.0f}s.")


def keep_awake(on: bool) -> None:
    """Stop Windows sleeping during a run. The monitor may still switch off (ES_DISPLAY_REQUIRED is not set)."""
    if os.name != "nt":
        return
    import ctypes

    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


def _listening_pid(port: int) -> int | None:
    """PID of the process listening on `port` (the debug Chrome's browser process)."""
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 5 and cols[3] == "LISTENING" and cols[1].rsplit(":", 1)[-1] == str(port):
            return int(cols[4])
    return None


def hide_from_taskbar(cdp_url: str) -> int:
    """Remove the debug Chrome's windows from the taskbar (Windows). Returns how many windows were changed.

    The windows are marked WS_EX_TOOLWINDOW instead of being hidden: a hidden window stops rendering,
    which would break screenshots, while a tool window renders normally but has no taskbar button.
    Call it again after opening a new tab/window, because those get their own taskbar buttons.
    """
    if os.name != "nt":
        return 0
    import ctypes
    from ctypes import wintypes

    pid = _listening_pid(urlparse(cdp_url).port or 9222)
    if pid is None:
        return 0
    user32 = ctypes.windll.user32
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000
    SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
    changed = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if not style & WS_EX_TOOLWINDOW or style & WS_EX_APPWINDOW:
                # The taskbar only re-reads the style when the window is hidden and shown again.
                user32.ShowWindow(hwnd, SW_HIDE)
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
                user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                changed.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    return len(changed)
