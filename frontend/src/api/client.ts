import type { JobDetail, JobStatus, JobSummary, LoginPortal, LoginStatus, Profile, SkillGaps, Stats } from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
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
  return res.json() as Promise<T>;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  listJobs: (status?: JobStatus) => request<JobSummary[]>(`/jobs${status ? `?status=${status}` : ""}`),
  getJob: (id: string) => request<JobDetail>(`/jobs/${id}`),
  stats: () => request<Stats>("/jobs/stats"),
  approve: (id: string) => post<{ id: string; status: JobStatus }>(`/jobs/${id}/approve`),
  reject: (id: string, reason?: string) => post<{ id: string; status: JobStatus }>(`/jobs/${id}/reject`, { reason }),
  remove: (id: string) => request<{ id: string; deleted: boolean }>(`/jobs/${id}`, { method: "DELETE" }),
  search: (source: string, limit: number) => post<Record<string, number>>("/search", { source, limit }),
  apply: (max_count: number) => post<Record<string, number>>("/apply", { max_count }),
  skillGaps: () => request<SkillGaps>("/skill-gaps"),
  loginStatus: (portal: LoginPortal) => request<LoginStatus>(`/login/${portal}`),
  loginStart: (portal: LoginPortal, email?: string, password?: string) =>
    post<LoginStatus>(`/login/${portal}`, { email, password }),
  loginDone: (portal: LoginPortal) => post<LoginStatus>(`/login/${portal}/done`),
  browserStatus: () => request<{ active: boolean; portals: string[]; viewing: string[] }>("/browser/status"),
  openPosting: (job_id: string) => post<{ portal: string; url: string }>("/browser/open", { job_id }),
  openInChrome: (job_id: string) => post<{ portal: string; url: string }>("/browser/open-chrome", { job_id }),
  closePosting: (portal: string) => post<{ portal: string; closing: boolean }>(`/browser/${portal}/close`),
  profile: () => request<Profile>("/profile"),
};
