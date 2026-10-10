import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useJobsStore } from "../store/jobsStore";
export default function JobList() {
    const jobs = useJobsStore((s) => s.jobs);
    const loading = useJobsStore((s) => s.loading);
    const selectedId = useJobsStore((s) => s.selectedId);
    const select = useJobsStore((s) => s.select);
    if (!loading && jobs.length === 0) {
        return _jsx("section", { className: "list empty", children: "No jobs here yet. Try \u201CSearch jobs\u201D." });
    }
    return (_jsx("section", { className: "list", children: jobs.map((j) => (_jsxs("button", { className: `card ${j.id === selectedId ? "selected" : ""}`, onClick: () => select(j.id), children: [_jsx("span", { className: `score ${scoreClass(j.match_score)}`, children: j.match_score ?? "–" }), _jsxs("span", { className: "meta", children: [_jsx("strong", { children: j.title }), _jsxs("small", { children: [j.company, " \u00B7 ", j.location || "n/a", " \u00B7 ", j.portal] })] })] }, j.id))) }));
}
function scoreClass(score) {
    if (score === null)
        return "none";
    return score >= 70 ? "good" : score >= 50 ? "mid" : "low";
}
