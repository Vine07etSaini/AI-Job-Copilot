import { jsxs as _jsxs } from "react/jsx-runtime";
import { STATUSES } from "../api/types";
import { useJobsStore } from "../store/jobsStore";
export default function StatusTabs() {
    const filter = useJobsStore((s) => s.filter);
    const setFilter = useJobsStore((s) => s.setFilter);
    const byStatus = useJobsStore((s) => s.stats?.by_status);
    const total = useJobsStore((s) => s.stats?.total ?? 0);
    return (_jsxs("nav", { className: "tabs", children: [_jsxs("button", { className: filter === "all" ? "active" : "", onClick: () => setFilter("all"), children: ["all (", total, ")"] }), STATUSES.map((st) => (_jsxs("button", { className: filter === st ? "active" : "", onClick: () => setFilter(st), children: [st.replace("_", " "), " (", byStatus?.[st] ?? 0, ")"] }, st)))] }));
}
