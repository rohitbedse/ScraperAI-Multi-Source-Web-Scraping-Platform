export const API = (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8000";
const KEY = (import.meta.env.VITE_API_KEY as string | undefined) || "";

export interface ParamSchema {
  type?: string;
  anyOf?: { type?: string }[];
  title?: string;
  description?: string;
  default?: unknown;
  minimum?: number;
  maximum?: number;
}
export interface Source {
  id: string;
  name: string;
  description: string;
  status: "available" | "unavailable";
  stages?: string[];
  params_schema: { properties?: Record<string, ParamSchema> };
}
export interface JobError {
  id: number;
  timestamp: string;
  stage: string;
  error_code: string;
  message: string;
  error_type: string | null;
  technical_message: string | null;
  file: string | null;
  function: string | null;
  line: number | null;
  retryable: boolean;
  job_continues: boolean;
  traceback: string | null;
}
export interface Job {
  id: string;
  source: string;
  status: "queued" | "running" | "cancelling" | "completed" | "partially_completed" | "failed" | "cancelled";
  params: Record<string, unknown>;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
  stage: string;
  percent: number;
  message: string;
  items_total: number | null;
  items_done: number;
  error_count: number;
  stats: { items?: number; errors?: number; duplicates?: number; invalid?: number } | null;
  has_output: boolean;
  download_url: string | null;
  errors: JobError[];
}
export interface JobEvent {
  stage: string;
  percent: number;
  items_done: number;
  items_total: number | null;
  message: string;
  event_type: string;
  status?: Job["status"];
  error_code?: string;
  timestamp: string;
}

export const isTerminal = (s: Job["status"]) => !["queued", "running", "cancelling"].includes(s);

export const withKey = (url: string) =>
  KEY ? `${url}${url.includes("?") ? "&" : "?"}api_key=${encodeURIComponent(KEY)}` : url;

async function req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (KEY) headers["X-API-Key"] = KEY;
  const r = await fetch(`${API}${path}`, { ...init, headers });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const j = await r.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch { /* keep statusText */ }
    throw new Error(detail);
  }
  return r.json();
}

export const api = {
  sources: () => req<Source[]>("/sources"),
  jobs: () => req<Job[]>("/jobs?limit=30"),
  job: (id: string, traceback = false) => req<Job>(`/jobs/${id}${traceback ? "?include_traceback=true" : ""}`),
  create: (source: string, params: Record<string, unknown>) =>
    req<Job>("/jobs", { method: "POST", body: JSON.stringify({ source, params }) }),
  cancel: (id: string) => req<Job>(`/jobs/${id}/cancel`, { method: "POST" }),
  rerun: (id: string) => req<Job>(`/jobs/${id}/rerun`, { method: "POST" }),
  downloadUrl: (id: string) => withKey(`${API}/jobs/${id}/download`),
  eventsUrl: (id: string) => withKey(`${API}/jobs/${id}/events`),
};
