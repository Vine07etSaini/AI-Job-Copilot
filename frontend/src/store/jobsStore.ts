import { create } from "zustand";
import { api } from "../api/client";
import type { JobDetail, JobStatus, JobSummary, SkillGaps, Stats } from "../api/types";
import { useToastStore } from "./toastStore";

interface JobsState {
  jobs: JobSummary[];
  stats: Stats | null;
  skillGaps: SkillGaps | null;
  filter: JobStatus | "all";
  selectedId: string | null;
  detail: JobDetail | null;
  loading: boolean;
  searching: string[]; // sources with a search in progress; different portals can search at the same time
  applying: boolean;

  setFilter: (f: JobStatus | "all") => Promise<void>;
  refresh: () => Promise<void>;
  select: (id: string | null) => Promise<void>;
  approve: (id: string) => Promise<void>;
  reject: (id: string) => Promise<void>;
  remove: (id: string) => Promise<void>;
  /** Per portal: the id of the job open in its live view ("" when a posting is open but we don't know which). */
  liveViewing: Record<string, string>;
  setLiveViewing: (portal: string, open: boolean) => void;
  openInLiveView: (id: string) => Promise<void>;
  closeLiveView: (portal: string, waitClosed?: boolean) => Promise<void>;
  openInChrome: (id: string) => Promise<void>;
  runSearch: (source: string, limit: number) => Promise<void>;
  applyApproved: (max: number) => Promise<void>;
}

const toast = (text: string, kind: "info" | "error" = "info") => useToastStore.getState().push(text, kind);
const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export const useJobsStore = create<JobsState>()((set, get) => ({
  jobs: [],
  stats: null,
  skillGaps: null,
  filter: "pending_review",
  selectedId: null,
  detail: null,
  loading: false,
  searching: [],
  liveViewing: {},
  applying: false,

  setFilter: async (filter) => {
    set({ filter });
    await get().refresh();
  },

  refresh: async () => {
    const { filter, selectedId } = get();
    set({ loading: true });
    try {
      const [jobs, stats, skillGaps] = await Promise.all([
        api.listJobs(filter === "all" ? undefined : filter),
        api.stats(),
        api.skillGaps(),
      ]);
      set({ jobs, stats, skillGaps });
      // The selected job may have left this filter; keep the detail pane in sync with the server.
      if (selectedId) set({ detail: await api.getJob(selectedId).catch(() => null) });
    } catch (e) {
      toast(errText(e), "error");
    } finally {
      set({ loading: false });
    }
  },

  select: async (id) => {
    set({ selectedId: id, detail: null });
    if (!id) return;
    try {
      const detail = await api.getJob(id);
      // Ignore a stale response if the user already clicked another job.
      if (get().selectedId === id) set({ detail });
    } catch (e) {
      toast(errText(e), "error");
    }
  },

  approve: async (id) => {
    try {
      await api.approve(id);
      toast("Approved. Use 'Apply approved' to actually apply.");
      await get().refresh();
    } catch (e) {
      toast(errText(e), "error");
    }
  },

  reject: async (id) => {
    try {
      await api.reject(id);
      toast("Rejected.");
      await get().refresh();
    } catch (e) {
      toast(errText(e), "error");
    }
  },

  remove: async (id) => {
    try {
      await api.remove(id);
      toast("Deleted.");
      if (get().selectedId === id) set({ selectedId: null, detail: null });
      await get().refresh();
    } catch (e) {
      toast(errText(e), "error");
    }
  },

  // Called from the live panels' status poll, so a posting closed elsewhere (panel button, 15-min timeout) shows here.
  setLiveViewing: (portal, open) => {
    const current = get().liveViewing;
    if (open && !(portal in current)) set({ liveViewing: { ...current, [portal]: "" } });
    if (!open && portal in current) {
      const { [portal]: _, ...rest } = current;
      set({ liveViewing: rest });
    }
  },

  openInLiveView: async (id) => {
    const job = get().jobs.find((j) => j.id === id) ?? get().detail;
    const portal = job?.portal;
    try {
      // One posting per portal window: opening another job there replaces the open one.
      if (portal && portal in get().liveViewing) await get().closeLiveView(portal, true);
      await api.openPosting(id);
      if (portal) set({ liveViewing: { ...get().liveViewing, [portal]: id } });
      toast(`Opened in the ${portal ?? ""} live view. Tick "Take control" to use it; "Close live view" when done.`);
      window.scrollTo({ top: 0, behavior: "smooth" }); // the live panels are at the top
    } catch (e) {
      toast(errText(e), "error");
    }
  },

  closeLiveView: async (portal, waitClosed = false) => {
    try {
      await api.closePosting(portal);
      // The window closes a moment later; when replacing it, wait so the portal is free again.
      for (let i = 0; waitClosed && i < 30 && (await api.browserStatus()).viewing.includes(portal); i++) {
        await new Promise((r) => setTimeout(r, 300));
      }
      get().setLiveViewing(portal, false);
    } catch (e) {
      toast(errText(e), "error");
    }
  },

  openInChrome: async (id) => {
    try {
      await api.openInChrome(id);
      toast("Opened in the copilot's Chrome (your saved logins). Look for the new Chrome window.");
    } catch (e) {
      toast(errText(e), "error");
    }
  },

  runSearch: async (source, limit) => {
    set({ searching: [...get().searching, source] });
    try {
      const stats = await api.search(source, limit);
      toast(`${source} search done: ${JSON.stringify(stats)}`);
      await get().refresh();
    } catch (e) {
      toast(errText(e), "error");
    } finally {
      set({ searching: get().searching.filter((s) => s !== source) });
    }
  },

  applyApproved: async (max) => {
    set({ applying: true });
    try {
      const stats = await api.apply(max);
      toast(`Apply done: ${JSON.stringify(stats)}`);
      await get().refresh();
    } catch (e) {
      toast(errText(e), "error");
    } finally {
      set({ applying: false });
    }
  },
}));
