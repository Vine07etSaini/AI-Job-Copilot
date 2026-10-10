import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
import { useEffect } from "react";
import { useJobsStore } from "../store/jobsStore";
import { useProfileStore } from "../store/profileStore";
export default function SideInfo() {
    const profile = useProfileStore((s) => s.profile);
    const loadProfile = useProfileStore((s) => s.load);
    const gaps = useJobsStore((s) => s.skillGaps);
    const appliedToday = useJobsStore((s) => s.stats?.applied_today ?? 0);
    useEffect(() => {
        void loadProfile();
    }, [loadProfile]);
    return (_jsxs("aside", { children: [_jsx("h3", { children: "Today" }), _jsxs("p", { children: [appliedToday, " applied"] }), _jsx("h3", { children: "Skill gaps" }), gaps && gaps.missing_skills.length > 0 ? (_jsx("ul", { children: gaps.missing_skills.map((g) => (_jsxs("li", { children: [g.skill, " ", _jsxs("small", { children: ["(", g.jobs, " jobs", g.learning ? ", learning" : "", ")"] })] }, g.skill))) })) : (_jsx("p", { className: "muted", children: "None yet." })), _jsx("h3", { children: "Profile" }), profile ? (_jsxs(_Fragment, { children: [_jsxs("p", { children: [profile.personal.name, _jsx("br", {}), _jsxs("small", { children: [profile.total_experience_years, " yrs \u00B7 ", profile.skills.length, " verified skills"] })] }), _jsx("div", { className: "tags", children: profile.skills.slice(0, 12).map((s) => (_jsx("span", { className: "tag", children: s.name }, s.name))) })] })) : (_jsx("p", { className: "muted", children: "Loading\u2026" }))] }));
}
