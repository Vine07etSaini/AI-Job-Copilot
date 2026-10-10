import { create } from "zustand";
import { api } from "../api/client";
import { useToastStore } from "./toastStore";
const toast = (text, kind = "info") => useToastStore.getState().push(text, kind);
const errText = (e) => (e instanceof Error ? e.message : String(e));
export const useJobsStore = create()((set, get) => ({
    jobs: [],
    stats: null,
    skillGaps: null,
    filter: "pending_review",
    selectedId: null,
    detail: null,
    loading: false,
    running: null,
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
            if (selectedId)
                set({ detail: await api.getJob(selectedId).catch(() => null) });
        }
        catch (e) {
            toast(errText(e), "error");
        }
        finally {
            set({ loading: false });
        }
    },
    select: async (id) => {
        set({ selectedId: id, detail: null });
        if (!id)
            return;
        try {
            const detail = await api.getJob(id);
            // Ignore a stale response if the user already clicked another job.
            if (get().selectedId === id)
                set({ detail });
        }
        catch (e) {
            toast(errText(e), "error");
        }
    },
    approve: async (id) => {
        try {
            await api.approve(id);
            toast("Approved. Use 'Apply approved' to actually apply.");
            await get().refresh();
        }
        catch (e) {
            toast(errText(e), "error");
        }
    },
    reject: async (id) => {
        try {
            await api.reject(id);
            toast("Rejected.");
            await get().refresh();
        }
        catch (e) {
            toast(errText(e), "error");
        }
    },
    runSearch: async (source, limit) => {
        set({ running: "search" });
        try {
            const stats = await api.search(source, limit);
            toast(`Search done: ${JSON.stringify(stats)}`);
            await get().refresh();
        }
        catch (e) {
            toast(errText(e), "error");
        }
        finally {
            set({ running: null });
        }
    },
    applyApproved: async (max) => {
        set({ running: "apply" });
        try {
            const stats = await api.apply(max);
            toast(`Apply done: ${JSON.stringify(stats)}`);
            await get().refresh();
        }
        catch (e) {
            toast(errText(e), "error");
        }
        finally {
            set({ running: null });
        }
    },
}));
