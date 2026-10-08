import { useEffect, useRef, useState } from "react";
import { api, isTerminal, Job, JobEvent } from "./api";

export const STAGES = ["INIT", "FETCH_LIST", "FETCH_DETAIL", "PARSE", "VALIDATE", "DEDUPE", "SAVE", "DONE"];

export interface LiveJob {
  job: Job | null;
  lastStage: string; // last normal pipeline stage reached (FAILED / PARTIAL are side exits)
  log: string[];
  error: string | null;
}

/** Loads a job, then follows its SSE stream. Replays history after a refresh/reconnect. */
export function useJob(id: string): LiveJob {
  const [job, setJob] = useState<Job | null>(null);
  const [lastStage, setLastStage] = useState("INIT");
  const [log, setLog] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const seen = useRef(0);

  useEffect(() => {
    let es: EventSource | null = null;
    let dead = false;
    setJob(null); setLog([]); setLastStage("INIT"); setError(null); seen.current = 0;

    const refresh = () => api.job(id).then((j) => !dead && setJob(j)).catch((e) => !dead && setError(String(e.message)));

    const onEvent = (e: MessageEvent) => {
      let ev: JobEvent;
      try { ev = JSON.parse(e.data); } catch { return; }
      if (STAGES.includes(ev.stage)) setLastStage(ev.stage);
      if (ev.message && ev.event_type !== "status") {
        setLog((l) => [...l.slice(-199), ev.message]);
      }
      setJob((j) => j && {
        ...j,
        stage: ev.stage,
        percent: Math.max(j.status === "running" ? j.percent : 0, ev.percent),
        items_done: ev.items_done,
        items_total: ev.items_total ?? j.items_total,
        message: ev.message || j.message,
        status: ev.status ?? j.status,
        error_count: ev.event_type === "error" && ev.error_code !== "CANCELLED" ? j.error_count + 1 : j.error_count,
      });
    };

    refresh().then(() => {
      if (dead) return;
      es = new EventSource(api.eventsUrl(id)); // browser auto-reconnects with Last-Event-ID
      for (const t of ["status", "stage", "progress", "log", "error", "done"]) es.addEventListener(t, onEvent as EventListener);
      es.addEventListener("end", () => { es?.close(); refresh(); });
      es.onerror = () => { if (es?.readyState === EventSource.CLOSED) refresh(); };
    });

    return () => { dead = true; es?.close(); };
  }, [id]);

  // error counts / summary come from the server once the job finishes
  useEffect(() => { if (job && isTerminal(job.status) && !job.stats && job.status !== "cancelled") api.job(id).then(setJob).catch(() => {}); }, [job?.status]);

  return { job, lastStage, log, error };
}
