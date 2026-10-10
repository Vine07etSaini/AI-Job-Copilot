import { useCallback, useEffect, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import { api } from "../api/client";

type Frame = { src: string; w: number; h: number };

const POLL_MS = 2000;
const MOVE_THROTTLE_MS = 50;

/** Live, interactive view of the automated browser: frames come in over a WebSocket, clicks and keys go back. */
export default function BrowserView() {
  const [frame, setFrame] = useState<Frame | null>(null);
  const [control, setControl] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const lastMove = useRef(0);

  // Poll the status; when a browser is open, connect, and reconnect if the socket drops.
  useEffect(() => {
    let stopped = false;
    let timer = 0;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${location.host}/api/browser/ws`);
      wsRef.current = ws;
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === "frame") setFrame({ src: `data:image/jpeg;base64,${msg.data}`, w: msg.w, h: msg.h });
        else if (msg.type === "idle") setFrame(null);
      };
      ws.onclose = () => {
        wsRef.current = null;
        setFrame(null);
      };
    };

    const tick = async () => {
      try {
        if ((await api.browserActive()).active && !wsRef.current) connect();
      } catch {
        // backend busy or down: try again next tick
      }
      if (!stopped) timer = window.setTimeout(tick, POLL_MS);
    };

    timer = window.setTimeout(tick, 0);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      wsRef.current?.close();
    };
  }, []);

  const send = useCallback((msg: object) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(JSON.stringify(msg));
  }, []);

  // Map a pointer position on the displayed image to page coordinates.
  const toPage = (e: ReactMouseEvent) => {
    const r = imgRef.current!.getBoundingClientRect();
    return { x: ((e.clientX - r.left) / r.width) * frame!.w, y: ((e.clientY - r.top) / r.height) * frame!.h };
  };

  if (!frame) return null;
  return (
    <section className="browser-view">
      <div className="browser-view-head">
        <h3>Automated browser (live)</h3>
        <label>
          <input type="checkbox" checked={control} onChange={(e) => setControl(e.target.checked)} /> Take control
        </label>
      </div>
      <div
        className={`browser-screen${control ? " controlling" : ""}`}
        tabIndex={0}
        onMouseDown={(e) => control && (e.currentTarget.focus(), send({ type: "mouse", action: "down", button: e.button, ...toPage(e) }))}
        onMouseUp={(e) => control && send({ type: "mouse", action: "up", button: e.button, ...toPage(e) })}
        onMouseMove={(e) => {
          if (!control || Date.now() - lastMove.current < MOVE_THROTTLE_MS) return;
          lastMove.current = Date.now();
          send({ type: "mouse", action: "move", ...toPage(e) });
        }}
        onWheel={(e) => control && send({ type: "mouse", action: "wheel", dx: e.deltaX, dy: e.deltaY, ...toPage(e) })}
        onContextMenu={(e) => control && e.preventDefault()}
        onKeyDown={(e) => {
          if (!control) return;
          e.preventDefault();
          send({ type: "key", key: e.key, ctrl: e.ctrlKey, alt: e.altKey, meta: e.metaKey, shift: e.shiftKey });
        }}
      >
        <img ref={imgRef} src={frame.src} alt="Live view of the automated browser" draggable={false} />
      </div>
      <small className="muted">
        {control ? "You are controlling the browser: click the picture, then type." : "View only. Tick “Take control” to click and type (for OTP or captcha)."}
      </small>
    </section>
  );
}
