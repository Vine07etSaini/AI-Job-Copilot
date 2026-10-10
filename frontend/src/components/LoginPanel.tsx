import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { LoginStatus } from "../api/types";
import { useToastStore } from "../store/toastStore";

/** Log in to Naukri from the UI: type details here (used once, never stored) or log in by hand in the live view. */
export default function LoginPanel() {
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState<LoginStatus>({ state: "idle", message: "" });
  const push = useToastStore((s) => s.push);
  const busy = status.state === "running" || status.state === "waiting";

  // Follow the login while it is in progress.
  useEffect(() => {
    if (!busy) return;
    const t = window.setInterval(() => void api.loginStatus().then(setStatus).catch(() => undefined), 1500);
    return () => window.clearInterval(t);
  }, [busy]);

  const start = async () => {
    try {
      setStatus(await api.loginStart(email || undefined, password || undefined));
      setPassword(""); // do not keep the password in the page after sending it
    } catch (e) {
      push(e instanceof ApiError ? e.message : "Could not start the login", "error");
    }
  };

  const done = async () => setStatus(await api.loginDone().catch(() => status));

  return (
    <div className="login-panel">
      <button onClick={() => setOpen(!open)}>{status.state === "logged_in" ? "Naukri: logged in ✓" : "Log in to Naukri"}</button>
      {open && (
        <div className="login-form">
          <input placeholder="Naukri email (optional)" value={email} onChange={(e) => setEmail(e.target.value)} disabled={busy} autoComplete="off" />
          <input type="password" placeholder="Password (optional)" value={password} onChange={(e) => setPassword(e.target.value)} disabled={busy} autoComplete="off" />
          <div className="login-buttons">
            <button className="primary" onClick={start} disabled={busy}>Open login</button>
            {busy && <button onClick={done}>Done</button>}
          </div>
          <small className="muted">
            {status.message || "Leave the fields empty to type your details into the live view. Details you enter here are used once and never saved."}
          </small>
        </div>
      )}
    </div>
  );
}
