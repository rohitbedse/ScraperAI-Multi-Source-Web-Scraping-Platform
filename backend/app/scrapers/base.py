"""Lightweight scraper contract. The runner only knows this interface."""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

from pydantic import BaseModel, ConfigDict

from app import config
from app.core.errors import FRIENDLY, RETRYABLE, classify, is_retryable, origin, technical_message
from app.schemas.events import ErrorCode, Event, EventType, Stage

DEFAULT_RANGES = {
    Stage.INIT: (0, 2), Stage.FETCH_LIST: (2, 20), Stage.FETCH_DETAIL: (20, 80),
    Stage.PARSE: (80, 85), Stage.VALIDATE: (85, 90), Stage.DEDUPE: (90, 95),
    Stage.SAVE: (95, 99), Stage.DONE: (100, 100),
}
DEFAULT_STAGES = [Stage.INIT, Stage.FETCH_LIST, Stage.FETCH_DETAIL, Stage.PARSE,
                  Stage.VALIDATE, Stage.DEDUPE, Stage.SAVE, Stage.DONE]


class NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScraperParseError(Exception):
    """The data we received does not have the expected shape."""


class LayoutChanged(Exception):
    """The page / API looks structurally different from what the scraper expects."""


class ScraperCancelled(BaseException):   # BaseException so a scraper's `except Exception` can't swallow it
    pass


def read_jsonl(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


class BaseScraper:
    # --- subclasses fill these in ---
    id: str = ""
    name: str = ""
    description: str = ""
    ParamsModel: type[BaseModel] = NoParams
    max_concurrent: int = 2                       # simultaneous jobs of this source
    stages: list[Stage] = DEFAULT_STAGES          # checklist order shown in the UI
    stage_ranges: dict = DEFAULT_RANGES           # overall percent range per stage

    def __init__(self, job_id: str, params: Optional[dict] = None, out=None):
        self.job_id = job_id
        self.params = self.ParamsModel(**(params or {}))
        self._out = out or sys.stderr          # event lines go here (the runner reads stdout)
        self.stage = Stage.INIT
        self.percent = 0.0
        self.items_done = 0
        self.items_total: Optional[int] = None
        self.error_count = 0
        self.saved = 0
        self.stats: dict = {"duplicates": 0, "invalid": 0}
        self.meta: dict = {}                   # small extras copied into the final JSON
        self.live_data: dict = {}              # optional extra counts attached to every event (Event.data)
        self.job_dir = config.ensure_job_dirs(job_id)
        self.partial_dir = self.job_dir / "partial"
        self.partial_path = self.partial_dir / "items.jsonl"
        self.output_path = self.job_dir / "output" / "result.json"
        self.cancelled = False
        self.on_cancel: Optional[Callable[[], None]] = None   # async scrapers hook in here
        self._last_progress = 0.0
        self._partial_fh = None
        self._in_finish = False

    # ---------------------------------------------------------------- to implement
    def run(self) -> None:
        raise NotImplementedError

    def commit(self) -> None:
        """Called after the job output is safely written; update master datasets here."""

    # ---------------------------------------------------------------- events
    def emit(self, event_type: EventType, message: str = "", **extra) -> None:
        extras = {k: extra.pop(k) for k in ("traceback", "stats", "output_file") if k in extra}
        ev = Event(job_id=self.job_id, source=self.id, stage=extra.pop("stage", self.stage),
                   percent=round(self.percent, 1), items_done=self.items_done,
                   items_total=self.items_total, message=message, event_type=event_type,
                   data=extra.pop("data", None) or (dict(self.live_data) or None), **extra)
        line = ev.model_dump(mode="json") | extras
        self._out.write(json.dumps(line, ensure_ascii=False) + "\n")
        self._out.flush()

    def set_stage(self, stage: Stage, message: str = "") -> None:
        self.check_cancelled()
        self.stage = stage
        self.items_done, self.items_total = 0, None
        self.percent = max(self.percent, self.stage_ranges.get(stage, (self.percent,))[0])
        self.emit(EventType.stage, message or stage.value.replace("_", " ").title())

    def set_percent(self, percent: float) -> None:
        """Move the bar inside the current stage's range (real milestones only)."""
        lo, hi = self.stage_ranges.get(self.stage, (self.percent, self.percent))
        self.percent = max(self.percent, min(hi, lo + percent * (hi - lo)))

    def progress(self, done: int, total: Optional[int] = None, message: str = "") -> None:
        """Report item progress. Percent only moves when the total is known (never faked)."""
        self.check_cancelled()
        self.items_done = done
        if total is not None:
            self.items_total = total
        if self.items_total:
            self.set_percent(min(done / self.items_total, 1))
        now = time.monotonic()
        if now - self._last_progress >= 0.2 or (self.items_total and done >= self.items_total):
            self._last_progress = now
            self.emit(EventType.progress, message)

    def log(self, message: str) -> None:
        self.emit(EventType.log, message)

    def report_error(self, code: ErrorCode, *, error_type: str, technical: str, tb: str = "",
                     message: str = "", stage: Optional[Stage] = None, retryable: Optional[bool] = None,
                     job_continues: bool = True, file=None, function=None, line=None) -> None:
        """Emit a structured error event; the traceback goes to the job log and job details only."""
        self.error_count += 1
        if tb:
            print(tb, file=sys.stderr, flush=True)
        self.emit(EventType.error, message or FRIENDLY[code], stage=stage or self.stage, error_code=code,
                  error_type=error_type, technical_message=technical[:600], file=file, function=function,
                  line=line, retryable=code in RETRYABLE if retryable is None else retryable,
                  job_continues=job_continues, traceback=tb)

    def handle_error(self, exc: BaseException, *, code: Optional[ErrorCode] = None, message: str = "",
                     stage: Optional[Stage] = None, retryable: Optional[bool] = None,
                     job_continues: bool = True) -> None:
        """Turn a real exception into a structured error event (type, message, traceback, stage)."""
        stage = stage or self.stage
        code = code or classify(exc, stage)
        f, fn, line = origin(exc)
        self.report_error(code, error_type=type(exc).__name__, technical=technical_message(exc),
                          tb="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                          message=message, stage=stage,
                          retryable=is_retryable(code, exc) if retryable is None else retryable,
                          job_continues=job_continues, file=f, function=fn, line=line)

    # ---------------------------------------------------------------- cancellation
    def request_cancel(self) -> None:
        """Called from the SIGTERM / CTRL_BREAK handler."""
        self.cancelled = True
        if self.on_cancel:
            self.on_cancel()

    def check_cancelled(self) -> None:
        if self.cancelled:
            raise ScraperCancelled()

    # ---------------------------------------------------------------- output
    def save_item(self, item: dict) -> None:
        """Append one result to partial/items.jsonl (never held in RAM)."""
        if self._partial_fh is None:
            self._partial_fh = open(self.partial_path, "a", encoding="utf-8")
        self._partial_fh.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        self._partial_fh.flush()
        self.saved += 1

    def append_jsonl(self, name: str, item: dict) -> None:
        """Scratch JSONL between stages (partial/<name>.jsonl), flushed per line."""
        with open(self.partial_dir / f"{name}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")

    def finalize_output(self) -> Path:
        """Stream partial JSONL -> output/result.json without loading it all."""
        if self._partial_fh:
            self._partial_fh.close()
            self._partial_fh = None
        head = {"job_id": self.job_id, "source": self.id,
                "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "total_items": self.saved, "stats": self.stats, **self.meta}
        tmp = self.output_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as out:
            out.write(json.dumps(head, ensure_ascii=False, indent=2)[:-2] + ',\n  "items": [')
            first = True
            if self.partial_path.exists():
                with open(self.partial_path, encoding="utf-8") as src:
                    for line in src:
                        if line.strip():
                            out.write(("" if first else ",") + "\n    " + line.strip())
                            first = False
            out.write("\n  ]\n}\n")
        os.replace(tmp, self.output_path)
        return self.output_path

    # ---------------------------------------------------------------- lifecycle
    def _finish(self, partial: bool) -> None:
        self._in_finish = True
        self.set_stage(Stage.SAVE, "Saving results")
        self.finalize_output()
        self.commit()
        self.stage = Stage.PARTIALLY_COMPLETED if partial else Stage.DONE
        self.percent = 100
        self.items_done = self.items_total = self.saved
        stats = {"items": self.saved, "errors": self.error_count, **self.stats}
        self.emit(EventType.done, f"Finished: {self.saved} items" +
                  (f" ({self.error_count} errors)" if self.error_count else ""),
                  stats=stats, output_file=self.output_path.name)

    def execute(self) -> int:
        """Run the scraper end to end; returns a process exit code."""
        try:
            self.set_stage(Stage.INIT, "Starting")
            self.run()
            self._finish(partial=self.error_count > 0)
            return 0
        except ScraperCancelled:
            self.report_error(ErrorCode.CANCELLED, error_type="ScraperCancelled",
                              technical="Cancelled by user", retryable=False, job_continues=False)
            return 3
        except BaseException as exc:  # noqa: BLE001
            self.handle_error(exc, job_continues=False)
            try:
                if self.saved and not self._in_finish:      # keep what we already have
                    self._finish(partial=True)
                    return 0
            except BaseException as save_exc:  # noqa: BLE001
                self.handle_error(save_exc, code=ErrorCode.SAVE_ERROR, stage=Stage.SAVE, job_continues=False)
            self.stage = Stage.FAILED
            self.emit(EventType.stage, "Failed")
            return 1
        finally:
            if self._partial_fh:
                self._partial_fh.close()
