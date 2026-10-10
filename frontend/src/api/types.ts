export type JobStatus =
  | "filtered_out"
  | "pending_review"
  | "approved"
  | "rejected"
  | "applied"
  | "needs_manual"
  | "failed";

export const STATUSES: JobStatus[] = [
  "pending_review",
  "approved",
  "applied",
  "needs_manual",
  "rejected",
  "failed",
  "filtered_out",
];

export interface JobSummary {
  id: string;
  title: string;
  company: string;
  portal: string;
  location: string | null;
  url: string;
  experience_text: string | null;
  salary_text: string | null;
  match_score: number | null;
  status: JobStatus;
  status_reason: string | null;
  resume_path: string | null;
  updated_at: string;
}

export interface MatchResult {
  score: number;
  matched_mandatory: string[];
  missing_mandatory: string[];
  matched_nice: string[];
  experience_ok: boolean;
  explanation: string;
}

export interface JobDetail extends JobSummary {
  description: string | null;
  recruiter_email: string | null;
  analysis: { summary?: string; keywords?: string[] } | null;
  match: MatchResult | null;
  guard: { dropped_skills?: string[]; notes?: string[] } | null;
}

export interface Stats {
  total: number;
  by_status: Partial<Record<JobStatus, number>>;
  applied_today: number;
}

export interface SkillGaps {
  jobs_analyzed: number;
  missing_skills: { skill: string; jobs: number; learning: boolean }[];
}

export interface Profile {
  personal: { name: string; email: string; location: string };
  summary: string;
  total_experience_years: number;
  skills: { name: string; years: number | null }[];
  learning_goals: string[];
  projects: { id: string; name: string; tech: string[]; on_resume: boolean; description: string }[];
}

export interface LoginStatus {
  state: "idle" | "running" | "waiting" | "logged_in" | "failed";
  message: string;
}
