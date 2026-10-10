import { jsx as _jsx } from "react/jsx-runtime";
import { useToastStore } from "../store/toastStore";
export default function Toasts() {
    const toasts = useToastStore((s) => s.toasts);
    const dismiss = useToastStore((s) => s.dismiss);
    return (_jsx("div", { className: "toasts", children: toasts.map((t) => (_jsx("div", { className: `toast ${t.kind}`, onClick: () => dismiss(t.id), children: t.text }, t.id))) }));
}
