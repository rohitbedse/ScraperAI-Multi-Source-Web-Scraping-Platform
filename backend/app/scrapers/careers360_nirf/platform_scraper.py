"""Platform wrapper around the Careers360 NIRF scraper (scraper.py / matcher.py / extractor.py stay unchanged).

INIT          load seed + parameters, resolve the selection (limit / only / resume), check live-access opt-in
FETCH_LIST    download (or reuse the cached) profile sitemap and match every selected college by name
FETCH_DETAIL  Careers360NirfScraper.run(): visit pages college by college (checkpoint + colleges_data.json
              are written after every college, so a stop or crash never loses finished work)
PARSE         re-read colleges_data.json through the CollegeRecord schema
VALIDATE      structural sanity check of each record
DEDUPE        course-level duplicates (counted by the core) + one record per seed id -> items.jsonl
SAVE          output/result.json (download endpoint); colleges_data.json + scrape_report.json copied beside it

The persistent work folder (checkpoint, colleges_data.json, scrape_report.json, scraper.log) is the CLI's own
output folder, so CLI and platform runs share one checkpoint.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.errors import classify
from app.schemas.events import ErrorCode, EventType, Stage
from app.scrapers.base import BaseScraper, ScraperCancelled
from app.scrapers.careers360_nirf import config
from app.scrapers.careers360_nirf.checkpoint import Checkpoint
from app.scrapers.careers360_nirf.http_client import (
    BlockedError, FetchError, HttpStatusError, PoliteClient, RobotsDisallowed)
from app.scrapers.careers360_nirf.matcher import load_sitemap_index, match_seed
from app.scrapers.careers360_nirf.page_discovery import LayoutChanged
from app.scrapers.careers360_nirf.schemas import CollegeRecord, DataFileError, Seed, load_parameters, load_seed
from app.scrapers.careers360_nirf.scraper import Careers360NirfScraper, ProgressEvent
from app.scrapers.careers360_nirf.storage import load_records

logger = logging.getLogger("scraper.careers360_nirf")

ALLOW_LIVE_ENV = "CAREERS360_ALLOW_LIVE"
MAX_REPORTED_PER_COLLEGE = 3         # error events per college; the rest are summarised in one event
FETCH_LIST_STAGES = {"matching"}


class Careers360Params(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(0, ge=0, le=100, title="College limit",
                       description="Only scrape the first N selected colleges (0 = all 100)")
    only: list[str] = Field(default_factory=list, max_length=100, title="Only these seed ids",
                            description="Comma separated NIRF seed ids, e.g. IR-O-U-0456 (empty = all)")
    resume: bool = Field(False, title="Resume",
                         description="Skip colleges already finished in the checkpoint; failed ones are retried")

    @field_validator("only")
    @classmethod
    def _valid_ids(cls, ids: list[str]) -> list[str]:
        ids = [i.strip() for i in ids if i and i.strip()]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate seed ids")
        try:
            known = {s.id for s in load_seed()}
        except DataFileError:
            return ids                     # reported properly in the INIT stage
        unknown = sorted(set(ids) - known)
        if unknown:
            raise ValueError(f"unknown seed id(s): {', '.join(unknown)}")
        return ids


class Careers360Error(Exception):
    """A failure with a platform error code and a clear, user-facing reason."""

    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def map_error_code(exc: BaseException, stage: Stage) -> ErrorCode:
    """Real exception -> platform error code (BLOCKED_403/429, NETWORK_TIMEOUT, PARSE_ERROR, ...)."""
    if isinstance(exc, Careers360Error):
        return exc.code
    if isinstance(exc, BlockedError):
        return ErrorCode.BLOCKED_429 if "429" in str(exc) else ErrorCode.BLOCKED_403
    if isinstance(exc, HttpStatusError):
        return {403: ErrorCode.BLOCKED_403, 429: ErrorCode.BLOCKED_429}.get(exc.status, ErrorCode.HTTP_ERROR)
    if isinstance(exc, FetchError):
        text = str(exc).lower()
        return ErrorCode.NETWORK_TIMEOUT if "timed out" in text or "timeout" in text else ErrorCode.HTTP_ERROR
    if isinstance(exc, RobotsDisallowed):
        return ErrorCode.HTTP_ERROR
    if isinstance(exc, LayoutChanged):
        return ErrorCode.PARSE_ERROR if "not valid JSON" in str(exc) else ErrorCode.LAYOUT_CHANGED
    if isinstance(exc, ValidationError):
        return ErrorCode.VALIDATION_FAILED
    return classify(exc, stage)


@dataclass
class _Captured:
    seed_id: Optional[str]
    core_stage: str
    url: str
    exc: Exception


class _LogCapture(logging.Handler):
    """Keeps ERROR log lines so an 'unexpected error' (which the core only logs) gets its real traceback."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class Careers360NirfPlatformScraper(BaseScraper):
    id = "careers360_nirf"
    name = "Careers360 – NIRF Top 100 Colleges"
    description = ("NIRF top-100 colleges matched to their Careers360 profile: college details, rankings and "
                   "UG/PG/PhD courses. Polite, robots.txt-aware, resumable. Needs "
                   f"{ALLOW_LIVE_ENV}=1 on the server (Careers360's terms restrict automated access).")
    ParamsModel = Careers360Params
    max_concurrent = 1
    stages = [Stage.INIT, Stage.FETCH_LIST, Stage.FETCH_DETAIL, Stage.PARSE, Stage.VALIDATE, Stage.DEDUPE,
              Stage.SAVE, Stage.DONE]
    stage_ranges = {
        Stage.INIT: (0, 2), Stage.FETCH_LIST: (2, 10), Stage.FETCH_DETAIL: (10, 90), Stage.PARSE: (90, 92),
        Stage.VALIDATE: (92, 95), Stage.DEDUPE: (95, 97), Stage.SAVE: (97, 99), Stage.DONE: (100, 100),
    }

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.work_dir: Path = config.OUTPUT_DIR          # persistent: checkpoint + colleges_data.json + log
        self.index = None                                 # SitemapIndex; loaded in FETCH_LIST when None
        self.core: Optional[Careers360NirfScraper] = None
        self._names: dict[str, str] = {}
        self._selected: list[Seed] = []
        self._done: set[str] = set()                      # seed ids with a final record in this job
        self._this_run: list[str] = []                    # ids actually scraped by this job (not resumed)
        self._captured: list[_Captured] = []
        self._current: Optional[str] = None
        self._core_stage = "starting"
        self._pages = 0
        self._last_block: Optional[ErrorCode] = None
        self._log_capture = _LogCapture()
        self._match_counts = {"matched": 0, "unmatched": 0, "ambiguous": 0}

    # ------------------------------------------------------------------ hooks for tests / alternate transports
    def make_client(self) -> PoliteClient:
        if os.environ.get(ALLOW_LIVE_ENV, "") not in ("1", "true", "yes"):
            raise Careers360Error(
                ErrorCode.SCRAPER_ERROR,
                f"Live access to careers360.com is not enabled. Its Terms of Use prohibit automated scraping; "
                f"if you have permission or accept the risk, set {ALLOW_LIVE_ENV}=1 for the backend and retry.")
        return PoliteClient(sleep=self._interruptible_sleep)

    def handle_error(self, exc, **kw):                    # clear message + code for our own failures
        if isinstance(exc, Careers360Error):
            kw.setdefault("code", exc.code)
            kw.setdefault("message", exc.message)
            kw.setdefault("retryable", exc.code == ErrorCode.BLOCKED_429)   # config / outcome errors: retry won't help
        super().handle_error(exc, **kw)

    # ------------------------------------------------------------------ pipeline
    def run(self) -> None:
        self._setup_logging()
        try:
            self._run()
        except ScraperCancelled:
            self._flush_partial()                         # checkpoint + colleges_data.json are already on disk
            raise
        finally:
            logging.getLogger("scraper.careers360_nirf").removeHandler(self._log_capture)

    def _run(self) -> None:
        p = self.params
        # ---- INIT
        try:
            seeds, params = load_seed(), load_parameters()
        except DataFileError as exc:
            raise Careers360Error(ErrorCode.SCRAPER_ERROR, f"Seed/parameter file problem: {exc}") from exc
        self._names = {s.id: s.name for s in seeds}
        selected = [s for s in seeds if not p.only or s.id in p.only]
        if p.limit:
            selected = selected[: p.limit]
        self._selected = selected
        cp = Checkpoint.load(self.work_dir / "checkpoint.json")
        self._done = {s.id for s in selected if p.resume and cp.is_done(s.id)}
        todo = [s for s in selected if s.id not in self._done]
        self.log(f"{len(selected)} colleges selected, {len(self._done)} already finished (resume), {len(todo)} to do")

        # ---- FETCH_LIST: sitemap + name matching
        self.set_stage(Stage.FETCH_LIST, "Matching colleges to Careers360 profiles")
        client = self.make_client()
        if self.index is None:
            try:
                self.index = load_sitemap_index(client, self.work_dir / "cache" / "sitemap_college_view.xml")
            except (BlockedError, HttpStatusError, FetchError, RobotsDisallowed) as exc:
                code = map_error_code(exc, Stage.FETCH_LIST)
                raise Careers360Error(code, f"Could not download the Careers360 profile sitemap: {exc}") from exc
        for i, seed in enumerate(todo, 1):
            self.check_cancelled()
            m = match_seed(seed, self.index)
            self._match_counts[m.status] += 1
            self._push(f"Matched {i}/{len(todo)}: {seed.name}", i, len(todo), core_stage="matching", current=seed.id)

        # ---- FETCH_DETAIL: the unchanged core scraper, one college at a time
        self.set_stage(Stage.FETCH_DETAIL, f"Scraping {len(todo)} colleges")
        core = Careers360NirfScraper(client, seeds, params, out_dir=self.work_dir, progress=self._on_core_progress,
                                     index=self.index)
        self.core = core
        real_fetch = core._fetch_state

        def observed_fetch(url: str, visited: list):      # one choke point for every page the core reads
            self.check_cancelled()
            try:
                state = real_fetch(url, visited)
            except Exception as exc:
                self._captured.append(_Captured(self._current, self._core_stage, url, exc))
                raise
            self._pages += 1
            self._push(f"{self._names.get(self._current, '')}: page {self._pages} read", len(self._done),
                       len(selected), core_stage=self._core_stage)
            return state

        core._fetch_state = observed_fetch
        self._push("Starting", len(self._done), len(selected), core_stage="starting")
        try:
            report = core.run(only=p.only or None, limit=p.limit or None, resume=p.resume)
        except DataFileError as exc:
            raise Careers360Error(ErrorCode.SCRAPER_ERROR, f"Existing Careers360 output is unreadable: {exc}") from exc
        self._after_run(report)

        # ---- PARSE
        self.set_stage(Stage.PARSE, "Reading the saved college records")
        try:
            records = load_records(core.colleges_file)
        except DataFileError as exc:
            raise Careers360Error(ErrorCode.PARSE_ERROR, str(exc)) from exc
        chosen = [records[s.id] for s in selected if s.id in records]
        self.progress(len(chosen), len(selected), f"Read {len(chosen)} college records")

        # ---- VALIDATE
        self.set_stage(Stage.VALIDATE, "Validating college records")
        valid: list[CollegeRecord] = []
        for i, rec in enumerate(chosen, 1):
            problem = self._record_problem(rec)
            if problem:
                self.stats["invalid"] += 1
                self.report_error(ErrorCode.VALIDATION_FAILED, error_type="InvalidRecord", technical=problem,
                                  message=f"{rec.seed.name} was dropped from the output: {problem}", retryable=False)
            else:
                valid.append(rec)
            self.progress(i, len(chosen))

        # ---- DEDUPE
        self.set_stage(Stage.DEDUPE, "Removing duplicates")
        seen: set[str] = set()
        profile_owner: dict[str, str] = {}
        for i, rec in enumerate(valid, 1):
            if rec.seed.id in seen:
                self.stats["duplicates"] += 1
            else:
                seen.add(rec.seed.id)
                self.stats["duplicates"] += rec.duplicates_skipped                    # courses dropped by the core
                if rec.match.url:
                    other = profile_owner.setdefault(rec.match.url, rec.seed.id)
                    if other != rec.seed.id:
                        self.log(f"{rec.seed.id} and {other} matched the same Careers360 page: {rec.match.url}")
                self.save_item(rec.model_dump(mode="json"))
            self.progress(i, len(valid))
        self.stats.update(self._final_counts())
        self.meta.update(run=report["run"], match=report["match"], scrape=report["scrape"],
                         courses_per_level=report["courses_per_level"])

    # ------------------------------------------------------------------ core progress -> platform events
    def _on_core_progress(self, ev: ProgressEvent) -> None:
        self._core_stage, self._current = ev.stage, ev.current_id
        if ev.stage == "saving":
            self._after_college(ev.current_id)
            self._last_progress = 0.0                    # always publish the result of a finished college
        elif ev.stage == "matching":
            self._pages = 0                              # page counter is per college
            self.check_cancelled()                       # between colleges: nothing half-done to lose
        elif ev.stage not in ("starting", "done", "aborted"):
            self.check_cancelled()
        name = self._names.get(ev.current_id or "", "")
        label = {"matching": "matching", "profile": "reading profile", "courses": "listing courses",
                 "admission": "reading admission", "course_details": "reading course pages",
                 "saving": "saved"}.get(ev.stage, ev.stage)
        self._push(f"{name}: {label}" if name else label, len(self._done), len(self._selected),
                   core_stage=ev.stage, current=ev.current_id)

    def _counts(self) -> dict:
        recs = [self.core.records[i] for i in self._done if self.core and i in self.core.records]
        total = len(self._selected)
        if self.core is None:                            # still in FETCH_LIST: name matching only
            return {"matched": self._match_counts["matched"], "unmatched": self._match_counts["unmatched"],
                    "ambiguous": self._match_counts["ambiguous"], "failed": 0, "pending": total,
                    "courses_extracted": 0}
        courses = sum(len(v) for r in recs for v in r.courses.values()) + getattr(self.core, "_extra_courses", 0)
        return {"matched": sum(r.match.status == "matched" for r in recs),
                "unmatched": sum(r.match.status == "unmatched" for r in recs),
                "ambiguous": sum(r.match.status == "ambiguous" for r in recs),
                "failed": sum(r.status == "failed" for r in recs),
                "pending": total - len(self._done), "courses_extracted": courses}

    def _push(self, message: str, done: int, total: int, *, core_stage: str, current: Optional[str] = None) -> None:
        self._core_stage = core_stage
        if current is not None:
            self._current = current
        self.live_data = {"processed": len(self._done), "total": len(self._selected), **self._counts(),
                          "current_id": self._current, "current_college": self._names.get(self._current or ""),
                          "step": core_stage}
        # not self.progress(): that raises on cancel, and this runs inside the core's saving / finally hooks
        self.items_done, self.items_total = done, total
        if total:
            self.set_percent(min(done / total, 1))
        now = time.monotonic()
        if now - self._last_progress >= 0.2:
            self._last_progress = now
            self.emit(EventType.progress, message)

    # ------------------------------------------------------------------ errors
    def _after_college(self, seed_id: Optional[str]) -> None:
        """A college just got its final record: report what really went wrong (if anything), then count it."""
        rec = self.core.records.get(seed_id) if self.core and seed_id else None
        captured, self._captured = self._captured, []
        logged, self._log_capture.messages = self._log_capture.messages, []
        if seed_id:
            self._done.add(seed_id)
            self._this_run.append(seed_id)
        if rec is None:
            return
        name = rec.seed.name
        stage_of = lambda c: Stage.FETCH_LIST if c.core_stage in FETCH_LIST_STAGES else Stage.FETCH_DETAIL
        reported = 0
        for c in captured:
            code = map_error_code(c.exc, stage_of(c))
            if code in (ErrorCode.BLOCKED_403, ErrorCode.BLOCKED_429):
                self._last_block = code
            if reported < MAX_REPORTED_PER_COLLEGE:
                self.handle_error(c.exc, code=code, stage=stage_of(c),
                                  message=f"{name}: {c.url.rsplit('/', 1)[-1] or c.url} - {_short(c.exc)}")
            reported += 1
        if reported > MAX_REPORTED_PER_COLLEGE:
            self.log(f"{name}: {reported - MAX_REPORTED_PER_COLLEGE} more page errors (see the job log)")
        unexpected = next((e for e in rec.errors if e.startswith("unexpected ")), None)
        if unexpected:                                    # a bug-type exception the core caught and logged
            tb = next((m.split("\n", 1)[1] for m in reversed(logged)
                       if f"{seed_id}: unexpected error" in m and "\n" in m), "")
            etype = unexpected.split(" ", 1)[1].split(":", 1)[0]
            self.report_error(ErrorCode.SCRAPER_ERROR, error_type=etype, technical=unexpected, tb=tb,
                              message=f"{name}: unexpected error while scraping", stage=Stage.FETCH_DETAIL,
                              retryable=True)
        elif rec.status == "failed" and not captured:
            self.report_error(ErrorCode.SCRAPER_ERROR, error_type="CollegeFailed",
                              technical="; ".join(rec.errors)[:500], message=f"{name} failed",
                              stage=Stage.FETCH_DETAIL, retryable=True)

    def _after_run(self, report: dict) -> None:
        """Whole-run outcome: a run that got nothing (blocked or otherwise) is a failed job, not a quiet success."""
        aborted = report["run"]["aborted"]
        ran = [self.core.records[i] for i in self._this_run if i in self.core.records]
        usable = [r for r in ran if r.status != "failed"]
        blocked = [r for r in ran if any(e.startswith("BLOCKED") for e in r.errors)]
        if ran and not usable:
            if blocked:
                code = self._last_block or ErrorCode.BLOCKED_403
                why = aborted or "every college was refused by the site"
                raise Careers360Error(code, f"Careers360 blocked the scraper ({why}). Nothing was scraped; the "
                                            f"block is not being worked around - try again later.")
            raise Careers360Error(ErrorCode.SCRAPER_ERROR,
                                  f"All {len(ran)} colleges failed. First error: {ran[0].errors[0] if ran[0].errors else '?'}")
        if aborted:
            code = self._last_block or ErrorCode.SCRAPER_ERROR
            self.report_error(code, error_type="RunAborted", technical=aborted,
                              message=f"Run stopped early: {aborted}. Finished colleges were kept; resume later.",
                              stage=Stage.FETCH_DETAIL)

    @staticmethod
    def _record_problem(rec: CollegeRecord) -> Optional[str]:
        if rec.status in ("scraped", "partial") and not (rec.match.url and rec.college):
            return "scraped record has no profile URL or college data"
        return None

    def _final_counts(self) -> dict:
        recs = [self.core.records[s.id] for s in self._selected if s.id in self.core.records]
        return {"colleges": len(recs),
                "matched": sum(r.match.status == "matched" for r in recs),
                "unmatched": sum(r.match.status == "unmatched" for r in recs),
                "ambiguous": sum(r.match.status == "ambiguous" for r in recs),
                "failed": sum(r.status == "failed" for r in recs),
                "courses_extracted": sum(len(v) for r in recs for v in r.courses.values())}

    # ------------------------------------------------------------------ cancellation / output
    def _interruptible_sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while True:
            self.check_cancelled()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.1, left))

    def _flush_partial(self) -> None:
        """Cancelled: write what is finished so far into the job output folder (not downloadable)."""
        try:
            if self.core is None:
                return
            for s in self._selected:
                if s.id in self._done and s.id in self.core.records:
                    self.save_item(self.core.records[s.id].model_dump(mode="json"))
            self.stats.update(self._final_counts())
            self.meta.update(cancelled=True)
            self.finalize_output()
            self.commit()
        except Exception:                                  # never mask the cancel
            logger.exception("could not flush partial output")

    def commit(self) -> None:
        """After result.json exists: keep the full CLI outputs next to it."""
        out = self.output_path.parent
        for name in ("colleges_data.json", "scrape_report.json"):
            src = self.work_dir / name
            if src.is_file():
                tmp = out / (name + ".tmp")
                shutil.copyfile(src, tmp)
                os.replace(tmp, out / name)

    def _setup_logging(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        lg = logging.getLogger("scraper.careers360_nirf")
        lg.setLevel(logging.INFO)
        log_file = str(self.work_dir / "scraper.log")
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        if not any(isinstance(h, logging.FileHandler) and h.baseFilename == str(Path(log_file).resolve())
                   for h in lg.handlers):
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(fmt)
            lg.addHandler(fh)
        if not any(getattr(h, "_job_stderr", False) for h in lg.handlers):
            sh = logging.StreamHandler(sys.stderr)       # stderr = the job log
            sh.setLevel(logging.WARNING)
            sh.setFormatter(fmt)
            sh._job_stderr = True
            lg.addHandler(sh)
        lg.addHandler(self._log_capture)


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:200]
