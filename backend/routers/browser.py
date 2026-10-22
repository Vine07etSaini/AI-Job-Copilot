"""Live views of the automated browser windows (one per portal): status and an interactive WebSocket view."""

from __future__ import annotations

import asyncio
import logging

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from backend import busy
from backend.deps import get_tracker
from job_copilot.config import get_settings
from job_copilot.db import Tracker
from job_copilot.portals import browser as browser_portal
from job_copilot.portals import live_view
from job_copilot.portals.base import BROWSER_PORTALS, PortalBlocked, get_portal

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/browser", tags=["browser"])

VIEW_TIMEOUT = 15 * 60  # an open posting closes by itself after this many seconds
OPEN_TIMEOUT = 90       # how long the request waits for the page to load before answering
# Postings opened for the user to look at, per portal: setting the event closes that window.
_viewers: dict[str, asyncio.Event] = {}


@router.get("/status")
def status() -> dict:
    """Which portals have a browser window open right now (each one can be watched over /ws?portal=...)."""
    portals = live_view.active_portals()
    return {"active": bool(portals), "portals": portals, "viewing": sorted(_viewers)}


class OpenRequest(BaseModel):
    job_id: str


@router.post("/open")
async def open_posting(body: OpenRequest, tracker: Annotated[Tracker, Depends(get_tracker)]) -> dict:
    """Open a job's posting in its portal's own browser window, shown in that portal's live view.

    The window stays open (and the portal busy) until POST /{portal}/close or VIEW_TIMEOUT.
    """
    job = tracker.get(body.job_id)
    if job is None:
        raise HTTPException(404, f"No job with id '{body.job_id}'.")
    portal = job["portal"]
    if portal not in BROWSER_PORTALS:
        raise HTTPException(400, f"'{portal}' jobs have no browser window; open the link in a new tab instead.")
    claimed = busy.acquire(portal)
    close = _viewers[portal] = asyncio.Event()
    opened: asyncio.Future = asyncio.get_running_loop().create_future()
    # If the request already gave up waiting, nobody reads a late failure; mark it read to avoid a warning.
    opened.add_done_callback(lambda f: f.cancelled() or f.exception())

    async def view() -> None:
        try:
            async with get_portal(portal, get_settings().portals.get(portal)) as site:
                await site.show(job["url"])
                opened.set_result(None)
                try:
                    await asyncio.wait_for(close.wait(), VIEW_TIMEOUT)
                except asyncio.TimeoutError:
                    log.info("Closing the %s posting after %ss", portal, VIEW_TIMEOUT)
        except Exception as e:  # reported to the request below (or logged if it already answered)
            if not opened.done():
                opened.set_exception(e)
            else:
                log.warning("%s live view ended with %r", portal, e)
        finally:
            _viewers.pop(portal, None)
            busy.release(claimed)

    asyncio.create_task(view())
    try:
        await asyncio.wait_for(asyncio.shield(opened), OPEN_TIMEOUT)
    except PortalBlocked as e:
        raise HTTPException(409, str(e)) from None
    except asyncio.TimeoutError:
        raise HTTPException(504, "The page is taking long to load; it stays open in the live view.") from None
    except Exception as e:
        raise HTTPException(502, f"Could not open the posting: {e}") from None
    return {"portal": portal, "url": job["url"]}


@router.post("/open-chrome")
async def open_in_chrome(body: OpenRequest, tracker: Annotated[Tracker, Depends(get_tracker)]) -> dict:
    """Open a job's posting in a normal, visible window of the copilot's Chrome (your saved logins) for you to use.

    Not a live view and not driven by the copilot, so it needs no busy claim and works for any job status.
    """
    job = tracker.get(body.job_id)
    if job is None:
        raise HTTPException(404, f"No job with id '{body.job_id}'.")
    portal = job["portal"]
    if portal not in BROWSER_PORTALS:
        raise HTTPException(400, f"'{portal}' jobs have no browser session; open the link in a new tab instead.")
    try:
        await browser_portal.open_in_chrome(job["url"], get_settings().portals[portal].cdp_url)
    except Exception as e:
        raise HTTPException(502, f"Could not open Chrome: {e}") from None
    return {"portal": portal, "url": job["url"]}


@router.post("/{portal}/close")
def close_posting(portal: str) -> dict:
    """Close a posting opened with /open (searches and logins close their own windows)."""
    if event := _viewers.get(portal):
        event.set()
    return {"portal": portal, "closing": event is not None}


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
async def interactive(ws: WebSocket, portal: str) -> None:
    """Stream one portal's page as JPEG frames and accept mouse/keyboard events for it."""
    if ws.headers.get("origin") not in ALLOWED_ORIGINS:
        await ws.close(code=1008)
        return
    await ws.accept()
    page = live_view.current_page(portal)
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
        # Stop when the portal closes or switches page (Indeed Apply opens its own tab): the client reconnects to it.
        while live_view.current_page(portal) is page:
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
            if (current := live_view.current_page(portal)) is not None:
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
