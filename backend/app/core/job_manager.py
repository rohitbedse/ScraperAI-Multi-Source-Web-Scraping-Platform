"""Queue + subprocess runner. Knows nothing about individual scrapers.

Flow: scraper subprocess -> stdout event lines -> this runner (the ONLY SQLite writer) -> SSE.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from typing import Optional

from sqlalchemy import select

from app import config
from app.core.errors import FRIENDLY
from app.db.models import Job, JobError, JobEvent, OutputFile
from app.db.session import session_scope
from app.schemas.events import (TERMINAL_STATUSES, ErrorCode, Event, EventType, JobStatus,
                                Stage, utcnow)
from app.scrapers.registry import REGISTRY, get_scraper_class, validate_params

logger = logging.getLogger("scraper.jobs")
TERMINAL = {s.value for s in TERMINAL_STATUSES}
ACTIVE = ("running", "cancelling")


def _kill_tree(proc: subprocess.Popen) -> None:
    """Last resort: kill the worker and everything it spawned (e.g. Chromium)."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(int(proc.pid)), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        proc.kill()


def _graceful_stop(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        proc.send_signal(signal.CTRL_BREAK_EVENT)      # worker handles SIGBREAK
    else:
        proc.send_signal(signal.SIGTERM)


class _Run:
    """Per-job state owned by its reader thread."""
    def __init__(self) -> None:
        self.fatal: Optional[dict] = None
        self.done: Optional[dict] = None
        self.last_saved = 0.0
        self.last_pct = -1.0


class JobManager:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.procs: dict[str, subprocess.Popen] = {}
        self.sources: dict[str, str] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.cancelled: set[str] = set()
        self._stop = threading.Event()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        config.ensure_dirs()
        self.reconcile(startup=True)
        self.cleanup_outputs()
        threading.Thread(target=self._housekeeping, daemon=True).start()
        self._pump()

    def shutdown(self) -> None:
        self._stop.set()
        with self.lock:
            procs = list(self.procs.values())
        for p in procs:
            _kill_tree(p)

    def _housekeeping(self) -> None:
        ticks = 0
        while not self._stop.wait(30):
            ticks += 1
            try:
                self.reconcile()
                if ticks % 120 == 0:
                    self.cleanup_outputs()
                self._pump()
            except Exception:
                logger.exception("housekeeping failed")

    def reconcile(self, startup: bool = False) -> None:
        """No job may stay running/cancelling without a live worker process."""
        with self.lock, session_scope() as s:
            for j in s.scalars(select(Job).where(Job.status.in_(ACTIVE))):
                if j.id in self.procs:
                    continue
                if j.status == "cancelling":
                    self._finish(s, j, JobStatus.cancelled, "Cancelled")
                else:
                    msg = "The server restarted while this job was running." if startup \
                        else FRIENDLY[ErrorCode.PROCESS_EXITED_UNEXPECTEDLY]
                    s.add(JobError(job_id=j.id, stage=j.stage, error_code=ErrorCode.PROCESS_EXITED_UNEXPECTEDLY.value,
                                   message=msg, error_type="OrphanedJob", retryable=True, job_continues=False,
                                   technical_message="No worker process was attached to this job."))
                    j.error_count += 1
                    self._finish(s, j, JobStatus.failed, msg)

    def cleanup_outputs(self) -> int:
        """Delete job folders older than OUTPUT_TTL_DAYS; mark their outputs expired."""
        cutoff = time.time() - config.OUTPUT_TTL_DAYS * 86400
        removed = 0
        for d in config.JOBS_DIR.glob("*"):
            try:
                if d.name in self.procs or not d.is_dir():
                    continue
                newest = max([f.stat().st_mtime for f in d.rglob("*") if f.is_file()] or [d.stat().st_mtime])
                if newest < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except OSError:
                pass
        with session_scope() as s:
            for o in s.scalars(select(OutputFile).where(OutputFile.expired.is_(False))):
                if not (config.job_dir(o.job_id) / "output" / o.filename).exists():
                    o.expired = True
                    j = s.get(Job, o.job_id)
                    if j:
                        j.output_path = None
        return removed

    # ------------------------------------------------------------------ public API
    def create_job(self, source: str, params: Optional[dict]) -> str:
        if source not in REGISTRY:
            raise KeyError(source)
        clean = validate_params(source, params)       # raises pydantic ValidationError
        job_id = uuid.uuid4().hex
        config.ensure_job_dirs(job_id)
        with session_scope() as s:
            j = Job(id=job_id, source=source, params=json.dumps(clean), status="queued", message="Queued")
            s.add(j)
            s.flush()
            self._add_event(s, j, EventType.status, "queued", stage=Stage.INIT, message="Queued")
        self._pump()
        return job_id

    def cancel(self, job_id: str) -> bool:
        """Cancel a queued/running job. False if it is already finished."""
        with self.lock:
            proc = self.procs.get(job_id)
            with session_scope() as s:
                j = s.get(Job, job_id)
                if not j or j.status in TERMINAL:
                    return False
                if proc is None:                       # still queued
                    self._finish(s, j, JobStatus.cancelled, "Cancelled before it started")
                    return True
                if job_id in self.cancelled:           # already cancelling
                    return True
                self.cancelled.add(job_id)
                j.status, j.message = "cancelling", "Cancelling - cleaning up"
                self._add_event(s, j, EventType.status, "cancelling", message=j.message)
        threading.Thread(target=self._terminate, args=(proc,), daemon=True).start()
        return True

    def _terminate(self, proc: subprocess.Popen) -> None:
        """SIGTERM, let the scraper close browsers / flush files, force-kill only if it hangs."""
        try:
            _graceful_stop(proc)
            proc.wait(timeout=config.CANCEL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning("worker %s ignored cancel for %ss - killing", proc.pid, config.CANCEL_GRACE_SECONDS)
            _kill_tree(proc)
        except Exception:
            logger.exception("graceful cancel failed - killing")
            _kill_tree(proc)

    # ------------------------------------------------------------------ scheduling
    def _pump(self) -> None:
        with self.lock:
            if len(self.procs) >= config.MAX_CONCURRENT_JOBS:
                return
            with session_scope() as s:
                per_source = Counter(self.sources.values())
                for j in s.scalars(select(Job).where(Job.status == "queued").order_by(Job.created_at)):
                    if len(self.procs) >= config.MAX_CONCURRENT_JOBS:
                        break
                    try:
                        limit = get_scraper_class(j.source).max_concurrent
                        if per_source[j.source] >= limit:
                            continue                       # this source is at its own limit
                        self._launch(s, j)
                        per_source[j.source] += 1
                    except Exception as exc:               # could not even spawn
                        logger.exception("could not start job %s", j.id)
                        s.add(JobError(job_id=j.id, stage="INIT", error_code=ErrorCode.SCRAPER_ERROR.value,
                                       message="The scraper could not be started.", error_type=type(exc).__name__,
                                       technical_message=str(exc)[:600], retryable=True, job_continues=False))
                        j.error_count += 1
                        self._finish(s, j, JobStatus.failed, "The scraper could not be started.")

    def _launch(self, s, j: Job) -> None:
        j.status, j.started_at, j.message = "running", utcnow(), "Starting"
        self._add_event(s, j, EventType.status, "running", stage=Stage.INIT, message="Started")
        log_path = config.ensure_job_dirs(j.id) / "logs" / "job.log"
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" \
            else {"start_new_session": True}
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        with open(log_path, "ab") as log:
            # argv list, no shell; job id / source / params were validated, never raw user input
            proc = subprocess.Popen(
                [sys.executable, "-m", "app.core.worker", j.id, j.source, j.params],
                cwd=str(config.BACKEND_DIR), stdout=subprocess.PIPE, stderr=log, env=env,
                text=True, encoding="utf-8", errors="replace", bufsize=1, **kw)
        self.procs[j.id] = proc
        self.sources[j.id] = j.source
        t = threading.Thread(target=self._reader, args=(j.id, proc), daemon=True)
        self.threads[j.id] = t
        t.start()

    # ------------------------------------------------------------------ per-job reader thread
    def _reader(self, job_id: str, proc: subprocess.Popen) -> None:
        run = _Run()
        log_path = config.job_dir(job_id) / "logs" / "job.log"
        try:
            for line in proc.stdout:
                try:
                    self._handle_line(job_id, line, run, log_path)
                except Exception:
                    logger.exception("job %s: failed to process event line", job_id)
        except Exception:
            logger.exception("job %s: reader crashed", job_id)
        finally:
            rc = proc.wait()
            try:
                self._finalize(job_id, rc, run)
            except Exception:
                logger.exception("job %s: finalize failed", job_id)
                self._force_fail(job_id)
            with self.lock:
                self.procs.pop(job_id, None)
                self.sources.pop(job_id, None)
                self.threads.pop(job_id, None)
                self.cancelled.discard(job_id)
            if not self._stop.is_set():
                self._pump()

    def _handle_line(self, job_id: str, line: str, run: _Run, log_path) -> None:
        try:
            d = json.loads(line)
            extras = {k: d.pop(k, None) for k in ("traceback", "stats", "output_file")}
            ev = Event(**d)
        except Exception:                                   # stray stdout -> keep it in the job log
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line)
            return
        now = time.monotonic()
        if ev.event_type == EventType.progress and now - run.last_saved < 1.0 \
                and abs(ev.percent - run.last_pct) < 1:
            return                                          # throttle persisted progress
        run.last_saved, run.last_pct = now, ev.percent
        with session_scope() as s:
            j = s.get(Job, job_id)
            if not j:
                return
            j.stage, j.percent, j.items_done, j.items_total = ev.stage.value, ev.percent, ev.items_done, ev.items_total
            if ev.message and j.status != "cancelling":
                j.message = ev.message
            self._add_event(s, j, ev.event_type, ev=ev)
            if ev.event_type == EventType.error and ev.error_code != ErrorCode.CANCELLED:
                j.error_count += 1
                s.add(JobError(job_id=job_id, stage=ev.stage.value, error_code=ev.error_code.value,
                               message=ev.message, error_type=ev.error_type,
                               technical_message=ev.technical_message, file=ev.file, function=ev.function,
                               line=ev.line, retryable=bool(ev.retryable), job_continues=bool(ev.job_continues),
                               traceback=extras["traceback"]))
                logger.error("job %s [%s] %s %s: %s\n%s", job_id, ev.stage.value, ev.error_code.value,
                             ev.error_type, ev.technical_message, extras["traceback"] or "")
                if not ev.job_continues:
                    run.fatal = {"msg": ev.message}
            elif ev.event_type == EventType.done:
                run.done = {"stage": ev.stage, "stats": extras["stats"], "file": extras["output_file"]}

    def _finalize(self, job_id: str, rc: int, run: _Run) -> None:
        with session_scope() as s:
            j = s.get(Job, job_id)
            if not j:
                return
            if run.done:                                    # finished its work, even if a cancel raced in
                name = run.done["file"] or ""
                path = config.job_dir(job_id) / "output" / name
                if name and path.exists():
                    j.output_path = name
                    s.add(OutputFile(job_id=job_id, filename=name, size_bytes=path.stat().st_size))
                j.stats = json.dumps(run.done["stats"])
                partial = run.done["stage"] == Stage.PARTIALLY_COMPLETED
                self._finish(s, j, JobStatus.partially_completed if partial else JobStatus.completed, j.message)
            elif job_id in self.cancelled:
                self._finish(s, j, JobStatus.cancelled, "Cancelled")
            elif run.fatal:
                self._finish(s, j, JobStatus.failed, run.fatal["msg"])
            else:                                           # died without telling us why
                tail = self._log_tail(job_id)
                last_line = next((l for l in reversed(tail.splitlines()) if l.strip()), "")
                msg = FRIENDLY[ErrorCode.PROCESS_EXITED_UNEXPECTEDLY]
                s.add(JobError(job_id=job_id, stage=j.stage, error_code=ErrorCode.PROCESS_EXITED_UNEXPECTEDLY.value,
                               message=msg, error_type="ProcessExit", retryable=True, job_continues=False,
                               technical_message=f"Worker exited with code {rc}. {last_line}"[:600],
                               traceback=tail or None))
                j.error_count += 1
                logger.error("job %s worker exited unexpectedly (code %s)\n%s", job_id, rc, tail)
                self._add_event(s, j, EventType.error, ev=Event(
                    job_id=job_id, source=j.source, stage=Stage(j.stage), percent=j.percent,
                    items_done=j.items_done, items_total=j.items_total, event_type=EventType.error, message=msg,
                    error_code=ErrorCode.PROCESS_EXITED_UNEXPECTEDLY, error_type="ProcessExit",
                    technical_message=f"Worker exited with code {rc}", retryable=True, job_continues=False))
                self._finish(s, j, JobStatus.failed, msg)

    def _force_fail(self, job_id: str) -> None:
        with session_scope() as s:
            j = s.get(Job, job_id)
            if j and j.status not in TERMINAL:
                self._finish(s, j, JobStatus.failed, "Internal error while finishing the job.")

    @staticmethod
    def _log_tail(job_id: str, lines: int = 30) -> str:
        try:
            with open(config.job_dir(job_id) / "logs" / "job.log", "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 6000))
                return "\n".join(f.read().decode("utf-8", "replace").splitlines()[-lines:])
        except OSError:
            return ""

    # ------------------------------------------------------------------ helpers
    def _finish(self, s, j: Job, status: JobStatus, message: str) -> None:
        j.status, j.completed_at, j.message = status.value, utcnow(), message
        stage = {JobStatus.failed: Stage.FAILED, JobStatus.partially_completed: Stage.PARTIALLY_COMPLETED,
                 JobStatus.completed: Stage.DONE}.get(status)
        if status == JobStatus.completed:
            j.percent = 100
        if stage:
            j.stage = stage.value
        self._add_event(s, j, EventType.status, status.value, stage=stage or Stage(j.stage), message=message)

    @staticmethod
    def _add_event(s, j: Job, event_type: EventType, status: Optional[str] = None, *,
                   ev: Optional[Event] = None, stage: Optional[Stage] = None, message: str = "") -> None:
        if ev is None:
            ev = Event(job_id=j.id, source=j.source, stage=stage or Stage(j.stage), percent=j.percent,
                       items_done=j.items_done, items_total=j.items_total, event_type=event_type,
                       message=message)
        payload = ev.model_dump(mode="json")
        if status:
            payload["status"] = status          # job state carried by status events
        s.add(JobEvent(job_id=j.id, event_type=event_type.value, payload=json.dumps(payload)))


manager = JobManager()
