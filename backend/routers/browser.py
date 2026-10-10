"""Live view of the automated browser: status and an interactive WebSocket view."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from job_copilot.portals import live_view

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/browser", tags=["browser"])


@router.get("/status")
def status() -> dict:
    return {"active": live_view.is_active()}


# ---- Interactive view: screencast out, mouse/keyboard in ----

# A browser lets any website open a WebSocket to localhost, so only our own frontend may connect.
ALLOWED_ORIGINS = {"http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8000", "http://127.0.0.1:8000"}
BUTTONS = {0: "left", 1: "middle", 2: "right"}
MODIFIERS = (("ctrl", "Control"), ("alt", "Alt"), ("meta", "Meta"), ("shift", "Shift"))


async def _apply_input(page, msg: dict) -> None:
    """Replay one input event from the frontend on the automated page."""
    kind = msg.get("type")
    if kind == "mouse":
        x, y, action = float(msg["x"]), float(msg["y"]), msg.get("action")
        await page.mouse.move(x, y)
        if action == "down":
            await page.mouse.down(button=BUTTONS.get(msg.get("button", 0), "left"))
        elif action == "up":
            await page.mouse.up(button=BUTTONS.get(msg.get("button", 0), "left"))
        elif action == "wheel":
            await page.mouse.wheel(float(msg.get("dx", 0)), float(msg.get("dy", 0)))
    elif kind == "key":
        key = str(msg.get("key", ""))
        mods = [name for flag, name in MODIFIERS if msg.get(flag)]
        if len(key) == 1 and not (msg.get("ctrl") or msg.get("alt") or msg.get("meta")):
            await page.keyboard.type(key)  # printable character, respects layout and shift
        elif key and key not in ("Control", "Alt", "Meta", "Shift"):
            await page.keyboard.press("+".join([*mods, key]))


@router.websocket("/ws")
async def interactive(ws: WebSocket) -> None:
    """Stream the automated page as JPEG frames and accept mouse/keyboard events."""
    if ws.headers.get("origin") not in ALLOWED_ORIGINS:
        await ws.close(code=1008)
        return
    await ws.accept()
    page = live_view.current_page()
    if page is None:
        await ws.send_json({"type": "idle"})
        await ws.close()
        return

    cdp = await page.context.new_cdp_session(page)
    frames: asyncio.Queue[dict] = asyncio.Queue(maxsize=2)

    def on_frame(params: dict) -> None:
        # Drop the oldest frame: a slow client should see fresh frames, not a backlog. This callback runs on the
        # event loop with no await in between, so it cannot actually race, but it must never raise (that would
        # stop the screencast), so the loop tolerates a queue that changed state anyway.
        while True:
            try:
                frames.put_nowait(params)
                break
            except asyncio.QueueFull:
                try:
                    frames.get_nowait()
                except asyncio.QueueEmpty:
                    pass
        asyncio.ensure_future(ack(params["sessionId"]))

    async def ack(session_id: int) -> None:
        try:  # Chrome sends no more frames until each one is acknowledged
            await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:  # the run ended and the page closed while the ack was in flight
            pass

    cdp.on("Page.screencastFrame", on_frame)
    await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 60, "maxWidth": 1280, "maxHeight": 900})

    async def send_frames() -> None:
        while live_view.is_active():
            try:
                params = await asyncio.wait_for(frames.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            meta = params["metadata"]
            await ws.send_json({"type": "frame", "data": params["data"],
                                "w": meta["deviceWidth"], "h": meta["deviceHeight"]})
        await ws.send_json({"type": "idle"})

    async def receive_input() -> None:
        while True:
            msg = await ws.receive_json()
            if (current := live_view.current_page()) is not None:
                try:
                    await _apply_input(current, msg)
                except Exception as e:  # a bad event or navigating page must not end the session
                    log.debug("Input event skipped: %s", e)

    tasks = [asyncio.create_task(send_frames()), asyncio.create_task(receive_input())]
    try:
        # asyncio.wait never re-raises a task's exception, so collect the results below.
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        # Retrieve every outcome so none is left as "Task exception was never retrieved". A client that
        # disconnects ends receive_input with WebSocketDisconnect (and a later send with RuntimeError): expected.
        for outcome in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(outcome, Exception) and not isinstance(outcome, (WebSocketDisconnect, RuntimeError)):
                log.warning("Live view task ended with %r", outcome)
        try:
            await cdp.send("Page.stopScreencast")
            await cdp.detach()
        except Exception:  # page already closed
            pass
