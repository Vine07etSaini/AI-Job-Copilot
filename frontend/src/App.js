import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useEffect } from "react";
import BrowserView from "./components/BrowserView";
import LoginPanel from "./components/LoginPanel";
import ActionsBar from "./components/ActionsBar";
import JobDetailPane from "./components/JobDetailPane";
import JobList from "./components/JobList";
import SideInfo from "./components/SideInfo";
import StatusTabs from "./components/StatusTabs";
import Toasts from "./components/Toasts";
import { useJobsStore } from "./store/jobsStore";
export default function App() {
    const refresh = useJobsStore((s) => s.refresh);
    useEffect(() => {
        void refresh();
    }, [refresh]);
    return (_jsxs("div", { className: "app", children: [_jsxs("header", { children: [_jsx("h1", { children: "AI Job Copilot" }), _jsxs("div", { className: "actions", children: [_jsx(LoginPanel, {}), _jsx(ActionsBar, {})] })] }), _jsx(BrowserView, {}), _jsx(StatusTabs, {}), _jsxs("main", { children: [_jsx(JobList, {}), _jsx(JobDetailPane, {}), _jsx(SideInfo, {})] }), _jsx(Toasts, {})] }));
}
