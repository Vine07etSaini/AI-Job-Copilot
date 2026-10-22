import { useState } from "react";
import { BROWSER_PORTALS } from "../api/types";
import { useJobsStore } from "../store/jobsStore";

export default function ActionsBar() {
  const searching = useJobsStore((s) => s.searching);
  const applying = useJobsStore((s) => s.applying);
  const runSearch = useJobsStore((s) => s.runSearch);
  const applyApproved = useJobsStore((s) => s.applyApproved);
  const approvedCount = useJobsStore((s) => s.stats?.by_status.approved ?? 0);
  const [source, setSource] = useState("sample");

  // Portals each have their own window, so another portal can start while one is busy, but not the same one twice.
  const portalsOf = (s: string) => (s === "all" ? BROWSER_PORTALS : [s]);
  const busy = new Set(searching.flatMap(portalsOf));
  const sourceBusy = portalsOf(source).some((p) => busy.has(p));

  return (
    <div className="actions">
      <select value={source} onChange={(e) => setSource(e.target.value)}>
        <option value="sample">sample (offline)</option>
        <option value="naukri">naukri (opens a browser)</option>
        <option value="indeed">indeed (opens a browser)</option>
        <option value="linkedin">linkedin (opens a browser)</option>
        <option value="all">all portals (in parallel)</option>
      </select>
      <button onClick={() => runSearch(source, 10)} disabled={sourceBusy}>
        {sourceBusy ? "Searching…" : "Search jobs"}
      </button>
      {searching.length > 0 && <small className="muted">running: {searching.join(", ")}</small>}
      <button
        className="primary"
        onClick={() => {
          // Applying touches the outside world, so always confirm.
          if (window.confirm(`Apply to up to 5 approved jobs (${approvedCount} approved)?`)) void applyApproved(5);
        }}
        disabled={applying || approvedCount === 0}
      >
        {applying ? "Applying…" : `Apply approved (${approvedCount})`}
      </button>
    </div>
  );
}
