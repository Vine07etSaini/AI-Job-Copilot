import { create } from "zustand";
let nextId = 1;
export const useToastStore = create()((set, get) => ({
    toasts: [],
    push: (text, kind = "info") => {
        const id = nextId++;
        set((s) => ({ toasts: [...s.toasts, { id, kind, text }] }));
        setTimeout(() => get().dismiss(id), 5000);
    },
    dismiss: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));
