import { useCallback, useEffect, useState } from "react";
import { api, Job, JobEvent, ParamSchema, Source, isTerminal } from "./api";
import { STAGES, useJob } from "./useJob";

const useHash = () => {
  const [h, setH] = useState(location.hash);
  useEffect(() => {
    const f = () => setH(location.hash);
    addEventListener("hashchange", f);
    return () => removeEventListener("hashchange", f);
  }, []);
  return h;
};

const STATUS_LABEL: Record<Job["status"], string> = {
  queued: "Queued", running: "Running", cancelling: "Cancelling", completed: "Completed",
  partially_completed: "Partially completed", failed: "Failed", cancelled: "Cancelled",
};
const STAGE_LABEL: Record<string, string> = {
  INIT: "Initialising", FETCH_LIST: "Fetch List", FETCH_DETAIL: "Fetch Details", PARSE: "Parse",
  VALIDATE: "Validate", DEDUPE: "Deduplicate", SAVE: "Save", DONE: "Done",
};
const stageLabel = (s: string) => STAGE_LABEL[s] ?? s.replace(/_/g, " ").toLowerCase();
const fmtDur = (s: number | null) => (s == null ? "-" : s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`);

function Badge({ status }: { status: Job["status"] }) {
  return <span className={`badge ${status}`}>{STATUS_LABEL[status]}</span>;
}

// ----------------------------------------------------------------- params form (schema driven)
function ParamsForm({ schema, value, onChange }: {
  schema: Record<string, ParamSchema>; value: Record<string, unknown>; onChange: (v: Record<string, unknown>) => void;
}) {
  return (
    <div className="params">
      {Object.entries(schema).map(([key, p]) => {
        const type = p.type ?? p.anyOf?.find((t) => t.type !== "null")?.type;
        const label = p.title ?? key;
        const isList = type === "array"; // entered comma separated, sent as a list
        return (
          <label key={key} title={p.description} className="param">
            <span>{label}</span>
            {type === "boolean" ? (
              <input type="checkbox" checked={!!value[key]} onChange={(e) => onChange({ ...value, [key]: e.target.checked })} />
            ) : (
              <input
                type={type === "integer" || type === "number" ? "number" : "text"}
                min={p.minimum} max={p.maximum}
                placeholder={p.default == null ? "optional" : String(p.default)}
                value={isList ? ((value[key] as string[] | undefined) ?? []).join(", ") : ((value[key] as string | number | undefined) ?? "")}
                onChange={(e) => {
                  const v = e.target.value;
                  const next = { ...value };
                  if (v === "") delete next[key];
                  else if (isList) next[key] = v.split(",").map((x) => x.trim()).filter(Boolean);
                  else next[key] = type === "integer" || type === "number" ? Number(v) : v;
                  onChange(next);
                }}
              />
            )}
          </label>
        );
      })}
    </div>
  );
}

// ----------------------------------------------------------------- dashboard
function SourceCard({ source, onRun }: { source: Source; onRun: (job: Job) => void }) {
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [params, setParams] = useState<Record<string, unknown>>({});
  const [err, setErr] = useState("");
  const props = source.params_schema.properties ?? {};
  const run = async () => {
    setBusy(true); setErr("");
    try { onRun(await api.create(source.id, params)); } catch (e) { setErr((e as Error).message); setBusy(false); }
  };
  return (
    <div className="card source">
      <div className="card-head">
        <span className="avatar">{source.name.slice(0, 1)}</span>
        <div className="grow">
          <h3>{source.name}</h3>
          <span className="src-id">{source.id}</span>
        </div>
        <span className={`pill ${source.status}`}>{source.status === "available" ? "Ready" : "Unavailable"}</span>
      </div>
      <p className="muted desc">{source.description}</p>
      {source.status === "unavailable" && source.reason && <p className="error-text reason">Reason: {source.reason}</p>}
      {open && <ParamsForm schema={props} value={params} onChange={setParams} />}
      {err && <p className="error-text">{err}</p>}
      <div className="row">
        <button className="btn primary" disabled={busy || source.status !== "available"} onClick={run}>
          {busy ? <span className="spinner" /> : "Run"}
        </button>
        {Object.keys(props).length > 0 && (
          <button className="btn ghost" onClick={() => setOpen(!open)}>{open ? "Hide options" : "Options"}</button>
        )}
      </div>
    </div>
  );
}

function JobRow({ job, names }: { job: Job; names: Record<string, string> }) {
  return (
    <a className="job-row" href={`#/jobs/${job.id}`}>
      <span className="c-name"><strong>{names[job.source] ?? job.source}</strong><small>{job.id.slice(0, 8)}</small></span>
      <span className="c-bar"><span className="mini"><i style={{ width: `${job.status === "completed" ? 100 : job.percent}%` }} /></span></span>
      <span className="c-num">{job.items_done.toLocaleString()} items</span>
      <span className="c-date">{new Date(job.created_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</span>
      <Badge status={job.status} />
    </a>
  );
}

function Dashboard() {
  const [sources, setSources] = useState<Source[] | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState("");
  const load = useCallback(() => api.jobs().then(setJobs).catch(() => {}), []);
  useEffect(() => {
    api.sources().then(setSources).catch((e) => setError(e.message));
    load();
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [load]);
  const names = Object.fromEntries((sources ?? []).map((s) => [s.id, s.name]));
  const count = (f: (j: Job) => boolean) => jobs.filter(f).length;
  const tiles: [string, number, string][] = [
    ["Total jobs", jobs.length, ""],
    ["Running", count((j) => j.status === "running" || j.status === "queued"), "run"],
    ["Completed", count((j) => j.status === "completed" || j.status === "partially_completed"), "ok"],
    ["Failed", count((j) => j.status === "failed"), "bad"],
  ];
  return (
    <>
      <div className="page-head">
        <h2 className="title">Data collection</h2>
        <p className="muted">Run scrapers, follow progress live and download structured results.</p>
      </div>
      <div className="tiles">
        {tiles.map(([label, n, tone]) => (
          <div key={label} className={`tile ${tone}`}><span>{label}</span><b>{n}</b></div>
        ))}
      </div>
      <h2>Sources</h2>
      {error && <p className="error-text">Cannot reach the backend: {error}</p>}
      {!sources && !error && <div className="skeleton" />}
      <div className="grid">
        {sources?.map((s) => <SourceCard key={s.id} source={s} onRun={(j) => (location.hash = `#/jobs/${j.id}`)} />)}
      </div>
      <h2>Recent jobs</h2>
      <div className="card list table">
        {jobs.length > 0 && <div className="job-row thead"><span className="c-name">Source</span><span className="c-bar">Progress</span><span className="c-num">Items</span><span className="c-date">Started</span><span>Status</span></div>}
        {jobs.length === 0 && <p className="muted pad">No jobs yet.</p>}
        {jobs.map((j) => <JobRow key={j.id} job={j} names={names} />)}
      </div>
    </>
  );
}

// ----------------------------------------------------------------- job view
function StageList({ stage, status, stages }: { stage: string; status: Job["status"]; stages: string[] }) {
  const idx = stages.indexOf(stage);
  return (
    <ul className="stages">
      {stages.map((s, i) => {
        const done = status === "completed" || i < idx;
        const current = !done && i === idx && !isTerminal(status);
        const broken = !done && i === idx && (status === "failed" || status === "cancelled");
        return (
          <li key={s} className={done ? "done" : current ? "current" : broken ? "broken" : ""}>
            <span className="mark">{done ? "✓" : current ? "●" : broken ? "✕" : "○"}</span>
            {stageLabel(s)}
          </li>
        );
      })}
    </ul>
  );
}

function ErrorDetails({ job }: { job: Job }) {
  const [full, setFull] = useState<Job | null>(null);
  const [open, setOpen] = useState(false);
  const toggle = () => {
    setOpen(!open);
    if (!full) api.job(job.id, true).then(setFull).catch(() => {});
  };
  const errs = (full ?? job).errors;
  return (
    <>
      <button className="btn ghost" onClick={toggle}>{open ? "Hide Details" : "View Details"}</button>
      {open && (
        <div className="details">
          {errs.map((e) => (
            <div key={e.id} className="err">
              <b>{e.error_code}</b> · {stageLabel(e.stage)} {e.file && <span className="muted">({e.file}:{e.line} {e.function})</span>}
              <div>{e.error_type}: {e.technical_message}</div>
              {e.traceback && <pre>{e.traceback}</pre>}
            </div>
          ))}
        </div>
      )}
    </>
  );
}

// Generic scraper-specific numbers: whatever the scraper puts in event.data (live) or stats (final).
const COUNT_LABELS: [string, string][] = [
  ["processed", "processed"], ["matched", "matched"], ["unmatched", "unmatched"], ["ambiguous", "ambiguous"],
  ["failed", "failed"], ["pending", "pending"], ["courses_extracted", "courses extracted"],
];
function LiveCounts({ data, stats, live }: { data: JobEvent["data"]; stats: Job["stats"]; live: boolean }) {
  const src = (live ? data : stats ?? data) as Record<string, number | string | null | undefined> | null;
  if (!src || !COUNT_LABELS.some(([k]) => typeof src[k] === "number")) return null;
  const total = typeof data?.total === "number" ? data.total : null;
  return (
    <>
      <div className="summary counts">
        {COUNT_LABELS.filter(([k]) => typeof src[k] === "number").map(([k, label]) => (
          <div key={k}><b>{(src[k] as number).toLocaleString()}{k === "processed" && total ? ` / ${total}` : ""}</b><span>{label}</span></div>
        ))}
      </div>
      {live && data?.current_college && <p className="muted">Now: {String(data.current_college)}{data.step ? ` (${String(data.step).replace(/_/g, " ")})` : ""}</p>}
    </>
  );
}

function JobView({ id }: { id: string }) {
  const { job, lastStage, log, data, error } = useJob(id);
  const [busy, setBusy] = useState(false);
  const [source, setSource] = useState<Source | null>(null);
  useEffect(() => { api.sources().then((all) => setSource(all.find((s) => s.id === job?.source) ?? null)).catch(() => {}); }, [job?.source]);
  if (error) return <p className="error-text">{error}</p>;
  if (!job) return <div className="skeleton tall" />;
  const stages = source?.stages ?? STAGES;
  const stage = stages.includes(job.stage) ? job.stage : lastStage;
  const live = !isTerminal(job.status);
  const act = async (f: () => Promise<Job>, go = false) => {
    setBusy(true);
    try { const j = await f(); if (go) location.hash = `#/jobs/${j.id}`; } catch (e) { alert((e as Error).message); }
    setBusy(false);
  };
  const lastErr = job.errors.filter((e) => !e.job_continues).at(-1) ?? job.errors.at(-1);
  return (
    <div className="card job">
      <div className="crumbs"><a href="#/">Dashboard</a> / <span>Job {job.id.slice(0, 8)}</span></div>
      <div className="card-head">
        <h3 className="grow">{source?.name ?? job.source}</h3>
        <Badge status={job.status} />
      </div>
      <div className={`bar ${live ? "live" : job.status}`}><div style={{ width: `${job.status === "completed" ? 100 : job.percent}%` }} /></div>
      <div className="row between muted">
        <span>{Math.round(job.status === "completed" ? 100 : job.percent)}%</span>
        <span>{job.items_done}{job.items_total ? ` / ${job.items_total}` : ""} items</span>
      </div>
      <StageList stage={stage} status={job.status} stages={stages} />
      <LiveCounts data={data} stats={job.stats} live={live} />
      {live && <p className="message">{job.message || "Waiting..."}{job.status === "queued" ? " (waiting for a free slot)" : ""}</p>}
      {job.status === "cancelling" && <p className="warn-text">Stopping the scraper and cleaning up...</p>}

      {(job.status === "completed" || job.status === "partially_completed") && (
        <div className="summary">
          <div><b>{job.stats?.items ?? job.items_done}</b><span>items scraped</span></div>
          <div><b>{job.stats?.duplicates ?? 0}</b><span>duplicates</span></div>
          <div><b>{job.stats?.invalid ?? 0}</b><span>invalid</span></div>
          <div><b>{job.error_count}</b><span>errors</span></div>
          <div><b>{fmtDur(job.duration_seconds)}</b><span>duration</span></div>
        </div>
      )}
      {job.status === "partially_completed" && <p className="warn-text">Finished with some problems: {lastErr?.message}</p>}
      {job.status === "failed" && (
        <div className="failure">
          <h4>{source?.name ?? job.source} Failed</h4>
          <dl>
            <dt>Stage</dt><dd>{stageLabel(lastErr?.stage ?? job.stage)}</dd>
            <dt>Reason</dt><dd>{lastErr?.message ?? job.message}</dd>
            <dt>Error</dt><dd>{lastErr?.technical_message ?? lastErr?.error_code ?? "Unknown"}</dd>
            <dt>Retryable</dt><dd>{lastErr?.retryable ? "Yes" : "No"}</dd>
          </dl>
        </div>
      )}
      {job.status === "cancelled" && <p className="muted">This job was cancelled.</p>}

      <div className="row">
        {live && <button className="btn danger" disabled={busy || job.status === "cancelling"} onClick={() => act(() => api.cancel(id))}>{job.status === "cancelling" ? "Cancelling..." : "Cancel"}</button>}
        {job.has_output && <a className="btn primary" href={api.downloadUrl(id)}>Download JSON</a>}
        {!live && <button className="btn" disabled={busy} onClick={() => act(() => api.rerun(id), true)}>{job.status === "failed" ? "Retry Job" : "Run again"}</button>}
        {job.errors.length > 0 && <ErrorDetails job={job} />}
        <a className="btn ghost" href="#/">Back</a>
      </div>
      {log.length > 0 && <pre className="log">{log.slice(-8).join("\n")}</pre>}
    </div>
  );
}

export default function App() {
  const hash = useHash();
  const m = hash.match(/^#\/jobs\/([0-9a-f]{32})$/);
  return (
    <div className="shell">
      <header className="topbar">
        <a href="#/" className="brand"><span className="logo">S</span><h1>Scraper Platform</h1></a>
        <nav><a href="#/" className={m ? "" : "active"}>Dashboard</a></nav>
      </header>
      <main>{m ? <JobView key={m[1]} id={m[1]} /> : <Dashboard />}</main>
    </div>
  );
}
