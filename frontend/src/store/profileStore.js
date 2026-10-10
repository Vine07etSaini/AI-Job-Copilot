import { create } from "zustand";
import { api } from "../api/client";
import { useToastStore } from "./toastStore";
export const useProfileStore = create()((set, get) => ({
    profile: null,
    load: async () => {
        if (get().profile)
            return; // the profile rarely changes; fetch once per page load
        try {
            set({ profile: await api.profile() });
        }
        catch (e) {
            useToastStore.getState().push(e instanceof Error ? e.message : String(e), "error");
        }
    },
}));
