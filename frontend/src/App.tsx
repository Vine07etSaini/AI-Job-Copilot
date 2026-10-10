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

  return (
    <div className="app">
      <header>
        <h1>AI Job Copilot</h1>
        <div className="actions">
          <LoginPanel />
          <ActionsBar />
        </div>
      </header>
      <BrowserView />
      <StatusTabs />
      <main>
        <JobList />
        <JobDetailPane />
        <SideInfo />
      </main>
      <Toasts />
    </div>
  );
}
