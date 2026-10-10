export class ApiError extends Error {
    constructor(status, message) {
        super(message);
        this.status = status;
    }
}
async function request(path, init) {
    const res = await fetch(`/api${path}`, {
        headers: { "Content-Type": "application/json" },
        ...init,
    });
    if (!res.ok) {
        // FastAPI errors look like {"detail": "..."} (or a list for 422 validation errors).
        const body = await res.json().catch(() => null);
        const detail = body?.detail;
        throw new ApiError(res.status, typeof detail === "string" ? detail : `Request failed (${res.status})`);
    }
    return res.json();
}
const post = (path, body) => request(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
export const api = {
    listJobs: (status) => request(`/jobs${status ? `?status=${status}` : ""}`),
    getJob: (id) => request(`/jobs/${id}`),
    stats: () => request("/jobs/stats"),
    approve: (id) => post(`/jobs/${id}/approve`),
    reject: (id, reason) => post(`/jobs/${id}/reject`, { reason }),
    search: (source, limit) => post("/search", { source, limit }),
    apply: (max_count) => post("/apply", { max_count }),
    skillGaps: () => request("/skill-gaps"),
    loginStatus: () => request("/login/naukri"),
    loginStart: (email, password) => post("/login/naukri", { email, password }),
    loginDone: () => post("/login/naukri/done"),
    browserActive: () => request("/browser/status"),
    profile: () => request("/profile"),
};
