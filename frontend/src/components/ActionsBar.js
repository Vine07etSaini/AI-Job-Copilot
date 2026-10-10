import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useState } from "react";
import { useJobsStore } from "../store/jobsStore";
export default function ActionsBar() {
    const running = useJobsStore((s) => s.running);
    const runSearch = useJobsStore((s) => s.runSearch);
    const applyApproved = useJobsStore((s) => s.applyApproved);
    const approvedCount = useJobsStore((s) => s.stats?.by_status.approved ?? 0);
    const [source, setSource] = useState("sample");
    return (_jsxs("div", { className: "actions", children: [_jsxs("select", { value: source, onChange: (e) => setSource(e.target.value), disabled: running !== null, children: [_jsx("option", { value: "sample", children: "sample (offline)" }), _jsx("option", { value: "naukri", children: "naukri (opens a browser)" })] }), _jsx("button", { onClick: () => runSearch(source, 10), disabled: running !== null, children: running === "search" ? "Searching…" : "Search jobs" }), _jsx("button", { className: "primary", onClick: () => {
                    // Applying touches the outside world, so always confirm.
                    if (window.confirm(`Apply to up to 5 approved jobs (${approvedCount} approved)?`))
                        void applyApproved(5);
                }, disabled: running !== null || approvedCount === 0, children: running === "apply" ? "Applying…" : `Apply approved (${approvedCount})` })] }));
}
