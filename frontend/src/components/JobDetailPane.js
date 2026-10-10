import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
import { useJobsStore } from "../store/jobsStore";
export default function JobDetailPane() {
    const detail = useJobsStore((s) => s.detail);
    const selectedId = useJobsStore((s) => s.selectedId);
    const approve = useJobsStore((s) => s.approve);
    const reject = useJobsStore((s) => s.reject);
    if (!selectedId)
        return _jsx("section", { className: "detail empty", children: "Select a job to review it." });
    if (!detail)
        return _jsx("section", { className: "detail empty", children: "Loading\u2026" });
    const canApprove = ["pending_review", "rejected", "needs_manual"].includes(detail.status);
    const canReject = !["applied", "rejected"].includes(detail.status);
    return (_jsxs("section", { className: "detail", children: [_jsx("h2", { children: detail.title }), _jsxs("p", { className: "sub", children: [detail.company, " \u00B7 ", detail.location || "n/a", " \u00B7 ", _jsx("span", { className: "badge", children: detail.status })] }), detail.status_reason && _jsx("p", { className: "reason", children: detail.status_reason }), _jsxs("div", { className: "buttons", children: [_jsx("button", { className: "primary", disabled: !canApprove, onClick: () => approve(detail.id), children: "Approve" }), _jsx("button", { disabled: !canReject, onClick: () => reject(detail.id), children: "Reject" }), detail.url && (_jsx("a", { href: detail.url, target: "_blank", rel: "noreferrer", children: "Open posting \u2197" }))] }), detail.match && (_jsxs(_Fragment, { children: [_jsxs("h3", { children: ["Match: ", detail.match.score, "/100"] }), _jsx("p", { children: detail.match.explanation }), _jsx(TagRow, { label: "Matched", items: detail.match.matched_mandatory, kind: "ok" }), _jsx(TagRow, { label: "Missing", items: detail.match.missing_mandatory, kind: "bad" }), _jsx(TagRow, { label: "Nice to have", items: detail.match.matched_nice, kind: "ok" })] })), detail.guard?.dropped_skills && detail.guard.dropped_skills.length > 0 && (_jsx(TagRow, { label: "Honesty Guard dropped", items: detail.guard.dropped_skills, kind: "bad" })), detail.resume_path && _jsxs("p", { className: "path", children: ["Resume: ", detail.resume_path] }), _jsx("h3", { children: "Description" }), _jsx("pre", { children: detail.description || "No description." })] }));
}
function TagRow({ label, items, kind }) {
    if (items.length === 0)
        return null;
    return (_jsxs("div", { className: "tags", children: [_jsxs("span", { children: [label, ":"] }), items.map((i) => (_jsx("span", { className: `tag ${kind}`, children: i }, i)))] }));
}
