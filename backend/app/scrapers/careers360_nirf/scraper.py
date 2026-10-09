"""Careers360 NIRF scraper: orchestration and CLI.

Run from `backend/` (the project uses absolute `app...` imports):
    python -m app.scrapers.careers360_nirf.scraper --allow-live --limit 3
    python -m app.scrapers.careers360_nirf.scraper --allow-live --only IR-O-U-0456 --resume

Careers360's Terms of Use forbid automated scraping, so the network is never touched
unless `--allow-live` is passed. See README.md.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.scrapers.careers360_nirf import config
from app.scrapers.careers360_nirf.checkpoint import Checkpoint
from app.scrapers.careers360_nirf.extractor import (
    CollegeContext, CourseContext, admission_map_from, build_course, classify_degree,
    course_dedupe_key, domain_map_from, extract_college, extract_destinations, extract_rankings,
    is_missing, page_location, required_pages)
from app.scrapers.careers360_nirf.http_client import (
    BlockedError, FetchError, HttpStatusError, PoliteClient, RobotsDisallowed)
from app.scrapers.careers360_nirf.matcher import SitemapIndex, load_sitemap_index, match_seed, verify_location
from app.scrapers.careers360_nirf.page_discovery import (
    LayoutChanged, course_detail, degree_filters, discover_pages, listing, parse_state, with_page)
from app.scrapers.careers360_nirf.report import build_report
from app.scrapers.careers360_nirf.schemas import (
    CollegeRecord, Course, DataFileError, MatchInfo, ParametersFile, Seed, load_parameters, load_seed)
from app.scrapers.careers360_nirf.storage import atomic_write_json, load_records, save_records

logger = logging.getLogger("scraper.careers360_nirf")


@dataclass
class ProgressEvent:
    """Passed to the progress callback so the platform can be connected later."""
    stage: str                  # starting|matching|profile|courses|admission|course_details|saving|done|aborted
    current_id: Optional[str]
    processed: int              # institutions with a final or failed record
    total: int                  # institutions in the seed file
    matched: int
    failed: int
    unmatched: int
    ambiguous: int
    pending: int
    courses_extracted: int


ProgressCallback = Callable[[ProgressEvent], None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Careers360NirfScraper:
    def __init__(self, client: PoliteClient, seeds: list[Seed], params: ParametersFile,
                 out_dir: Path = config.OUTPUT_DIR, progress: Optional[ProgressCallback] = None,
                 index: Optional[SitemapIndex] = None, max_detail_pages: int = config.MAX_COURSE_DETAIL_PAGES):
        self.client, self.seeds, self.params = client, seeds, params
        self.out_dir = Path(out_dir)
        self.colleges_file = self.out_dir / "colleges_data.json"
        self.report_file = self.out_dir / "scrape_report.json"
        self.checkpoint_file = self.out_dir / "checkpoint.json"
        self.sitemap_cache = self.out_dir / "cache" / "sitemap_college_view.xml"
        self.progress, self.index, self.max_detail_pages = progress, index, max_detail_pages
        self.need = required_pages(params)
        self.records: dict[str, CollegeRecord] = {}
        self._extra_courses = 0          # courses of the college currently being scraped

    # ============================================================================ run
    def run(self, only: Optional[list[str]] = None, limit: Optional[int] = None, resume: bool = False) -> dict:
        started = time.time()
        started_at = _now()
        self.records = load_records(self.colleges_file)
        cp = Checkpoint.load(self.checkpoint_file)

        selected = [s for s in self.seeds if not only or s.id in only]
        unknown = set(only or []) - {s.id for s in self.seeds}
        if unknown:
            raise DataFileError(f"--only: unknown seed id(s) {sorted(unknown)}")
        if limit is not None:
            selected = selected[:limit]
        todo = [s for s in selected if not (resume and cp.is_done(s.id))]
        skipped = len(selected) - len(todo)
        if skipped:
            logger.info("resume: skipping %d finished colleges", skipped)

        aborted: Optional[str] = None
        blocked_in_row = 0
        processed_now = 0
        self._emit("starting", None)
        try:
            for seed in todo:
                try:
                    rec = self.scrape_college(seed)
                except BlockedError as exc:               # blocked before we even had a profile (e.g. sitemap)
                    rec = self._failed(seed, f"BLOCKED: {exc}")
                except Exception as exc:                  # one bad college must not stop the run
                    logger.error("%s: unexpected error: %s\n%s", seed.id, exc, traceback.format_exc())
                    rec = self._failed(seed, f"unexpected {type(exc).__name__}: {exc}")
                self._extra_courses = 0
                self.records[seed.id] = rec
                self._emit("saving", seed.id)
                save_records(self.colleges_file, (self.records[s.id] for s in self.seeds if s.id in self.records))
                cp.mark(seed.id, rec.status, "; ".join(rec.errors[:3]) or None)
                cp.save()
                processed_now += 1
                logger.info("%s %s -> %s (%d courses)", seed.id, seed.name, rec.status,
                            sum(len(v) for v in rec.courses.values()))
                blocked_in_row = blocked_in_row + 1 if any(e.startswith("BLOCKED") for e in rec.errors) else 0
                if blocked_in_row >= config.MAX_CONSECUTIVE_BLOCKED_COLLEGES:
                    aborted = f"{blocked_in_row} colleges in a row were blocked by the site; stopping the run"
                    logger.error(aborted)
                    break
        except KeyboardInterrupt:
            aborted = "interrupted by user"
        finally:
            self._emit("aborted" if aborted else "done", None)
            run_info = {"started_at": started_at, "finished_at": _now(),
                        "duration_seconds": round(time.time() - started, 1),
                        "selected": len(selected), "processed_this_run": processed_now, "skipped_resume": skipped,
                        "requests_made": self.client.requests_made, "blocked_responses": self.client.blocked_count,
                        "aborted": aborted, "resume": resume}
            report = build_report(self.seeds, self.records, self.params, run_info)
            atomic_write_json(self.report_file, report)
        return report

    # ==================================================================== progress
    def _counts(self) -> dict[str, int]:
        recs = self.records.values()
        return {"matched": sum(r.match.status == "matched" for r in recs),
                "failed": sum(r.status == "failed" for r in recs),
                "unmatched": sum(r.match.status == "unmatched" for r in recs),
                "ambiguous": sum(r.match.status == "ambiguous" for r in recs)}

    def _emit(self, stage: str, current: Optional[str]) -> None:
        if not self.progress:
            return
        c = self._counts()
        total = len(self.seeds)
        processed = sum(1 for s in self.seeds if s.id in self.records)
        courses = sum(len(v) for r in self.records.values() for v in r.courses.values()) + self._extra_courses
        self.progress(ProgressEvent(stage=stage, current_id=current, processed=processed, total=total,
                                    pending=total - processed, courses_extracted=courses, **c))

    # ================================================================= one college
    def _failed(self, seed: Seed, error: str, match: Optional[MatchInfo] = None) -> CollegeRecord:
        return CollegeRecord(seed=seed, match=match or MatchInfo(status="unmatched", reason="not attempted"),
                             status="failed", errors=[error], scraped_at=_now(),
                             courses={lvl: [] for lvl in self.params.degree_levels})

    def _fetch_state(self, url: str, visited: list[str]) -> dict:
        html = self.client.get(url)
        visited.append(url)
        return parse_state(html)

    def scrape_college(self, seed: Seed) -> CollegeRecord:
        levels = self.params.degree_levels
        empty_courses = {lvl: [] for lvl in levels}

        # ---- 1. match
        self._emit("matching", seed.id)
        if self.index is None:
            self.index = load_sitemap_index(self.client, self.sitemap_cache)
        match = match_seed(seed, self.index)
        visited: list[str] = []
        errors: list[str] = []
        overview: Optional[dict] = None
        if match.status == "ambiguous" and 0 < len(match.candidates) <= config.AMBIGUOUS_PROBE_LIMIT:
            try:
                match, overview = self._resolve_ambiguous(seed, match, visited)
            except BlockedError as exc:
                return self._failed(seed, f"BLOCKED: {exc}", match)
        if match.status != "matched":
            logger.warning("%s %s: %s (%s)", seed.id, seed.name, match.status, match.reason)
            return CollegeRecord(seed=seed, match=match, status=match.status, courses=empty_courses,
                                 pages_visited=visited, scraped_at=_now())

        # ---- 2. profile page + identity check
        self._emit("profile", seed.id)
        if overview is None:
            try:
                overview = self._fetch_state(match.url, visited)
            except BlockedError as exc:
                return self._failed(seed, f"BLOCKED: {exc}", match)
            except (HttpStatusError, FetchError, RobotsDisallowed, LayoutChanged) as exc:
                logger.error("%s: profile page failed: %s", seed.id, exc)
                return self._failed(seed, f"profile page: {exc}", match)

        city, state = page_location(overview)
        state_ok, city_ok = verify_location(seed, city, state)
        match = match.model_copy(update={"state_verified": state_ok, "city_match": city_ok})
        if state_ok is False:
            match = match.model_copy(update={
                "status": "ambiguous",
                "reason": f"name matched but profile state {state!r} != seed state {seed.state!r}",
                "candidates": [*match.candidates[:1]]})
            logger.warning("%s %s: %s", seed.id, seed.name, match.reason)
            return CollegeRecord(seed=seed, match=match, status="ambiguous", courses=empty_courses,
                                 pages_visited=visited, scraped_at=_now())

        # ---- 3. college-level fields
        ctx = CollegeContext(seed=seed, profile_url=match.url, overview=overview)
        pages = discover_pages(overview, match.url)
        for kind in sorted(self.need - {"course_detail"}):
            if kind not in pages:
                errors.append(f"page not listed by Careers360: {kind}")
        college = extract_college(ctx, self.params.college_parameters)
        rankings = extract_rankings(ctx, self.params.ranking_category)
        destinations = extract_destinations(ctx, self.params.study_destination)

        # ---- 4. courses
        courses: dict[str, list[Course]] = empty_courses
        dup = other_level = detail_skipped = 0
        status_override: Optional[str] = None
        if "courses" in self.need and "courses" in pages:
            try:
                courses, dup, other_level, detail_skipped = self._collect_courses(seed, pages, visited, errors)
            except BlockedError as exc:
                errors.append(f"BLOCKED: {exc}")
                status_override = "failed"
            except (FetchError, HttpStatusError, RobotsDisallowed, LayoutChanged) as exc:
                logger.error("%s: course collection failed: %s", seed.id, exc)
                errors.append(f"courses: {exc}")

        # ---- 5. assemble
        missing = [k for grp in (college, rankings, destinations) for k, v in grp.items() if is_missing(v)]
        missing += sorted({f"course.{k}" for lst in courses.values() for c in lst for k in c.missing_fields})
        missing += [f"courses.{lvl}" for lvl, lst in courses.items() if not lst]
        if status_override:
            status = status_override
        elif errors or detail_skipped or ("courses" in self.need and not any(courses.values())):
            status = "partial"
        else:
            status = "scraped"
        return CollegeRecord(seed=seed, match=match, status=status, college=college, courses=courses,
                             rankings=rankings, study_destination=destinations, missing_fields=missing,
                             errors=errors, duplicates_skipped=dup, courses_skipped_other_level=other_level,
                             course_details_skipped=detail_skipped, pages_visited=visited, scraped_at=_now())

    def _resolve_ambiguous(self, seed: Seed, match: MatchInfo, visited: list[str]):
        """Close name ties (campuses of one brand): open each candidate's own profile and accept one
        only if exactly one states the seed's city AND state. Otherwise stay ambiguous."""
        passing: list[tuple[str, dict]] = []
        for cand in match.candidates[:config.AMBIGUOUS_PROBE_LIMIT]:
            try:
                st = self._fetch_state(cand.url, visited)
            except BlockedError:
                raise
            except (HttpStatusError, FetchError, RobotsDisallowed, LayoutChanged) as exc:
                logger.warning("%s: candidate %s unreadable: %s", seed.id, cand.url, exc)
                continue
            state_ok, city_ok = verify_location(seed, *page_location(st))
            if state_ok and city_ok:
                passing.append((cand.url, st))
        if len(passing) == 1:
            url, st = passing[0]
            score = next(c.score for c in match.candidates if c.url == url)
            return match.model_copy(update={
                "status": "matched", "url": url, "score": score, "state_verified": True, "city_match": True,
                "reason": "close name ties; only this candidate's profile states the seed's city and state"}), st
        reason = (f"{match.reason}; profile check: {len(passing)} of {len(match.candidates[:config.AMBIGUOUS_PROBE_LIMIT])} "
                  f"candidates state the seed's city and state")
        return match.model_copy(update={"reason": reason}), None

    # ================================================================== courses
    def _collect_courses(self, seed: Seed, pages: dict[str, str], visited: list[str], errors: list[str]):
        levels = self.params.degree_levels
        fields = self.params.course_fields

        self._emit("courses", seed.id)
        first = self._fetch_state(pages["courses"], visited)
        domain_map = domain_map_from(first)
        counts = {d["id"]: d.get("total_count", 0)
                  for d in (first.get("courseFeesDetail", {}).get("courseFilters") or {}).get("degree") or []}
        degrees = degree_filters(first)
        if not degrees:
            errors.append("courses page lists no degree filters; cannot assign UG/PG/PhD levels")
            return {lvl: [] for lvl in levels}, 0, 0, 0

        rows_by_level: dict[str, list[tuple[dict, str]]] = {lvl: [] for lvl in levels}
        seen_ids: set = set()
        dup = other_level = 0
        for d in degrees:
            level = classify_degree(d["label"])
            if level not in levels:
                other_level += int(counts.get(d["id"], 0) or 0)
                logger.info("%s: degree %r not UG/PG/PhD, skipped", seed.id, d["label"])
                continue
            page = 1
            while True:
                try:
                    st = self._fetch_state(with_page(d["url"], page), visited)
                except (HttpStatusError, FetchError, LayoutChanged) as exc:
                    errors.append(f"degree listing {d['label']} page {page}: {exc}")
                    break
                rows, total_pages = listing(st)
                for row in rows:
                    if row.get("id") in seen_ids:
                        dup += 1
                        continue
                    seen_ids.add(row.get("id"))
                    rows_by_level[level].append((row, d["label"]))
                    self._extra_courses += 1
                if not rows or page >= total_pages or page >= config.MAX_LISTING_PAGES_PER_DEGREE:
                    break
                page += 1
            self._emit("courses", seed.id)

        admission_map: dict[int, str] = {}
        if "admission" in self.need and "admission" in pages:
            self._emit("admission", seed.id)
            try:
                admission_map = admission_map_from(self._fetch_state(pages["admission"], visited))
            except (HttpStatusError, FetchError, LayoutChanged) as exc:
                errors.append(f"admission page: {exc}")
        cctx = CourseContext(domain_map=domain_map, admission_map=admission_map)

        # build without details first so duplicates are dropped before any per-course request
        built: dict[str, list[tuple[dict, Course]]] = {lvl: [] for lvl in levels}
        keys: set = set()
        for lvl in levels:
            for row, degree in rows_by_level[lvl]:
                course = build_course(row, degree, cctx, fields)
                key = course_dedupe_key(lvl, course)
                if key in keys:
                    dup += 1
                    continue
                keys.add(key)
                built[lvl].append((row, course))

        detail_skipped = 0
        if "course_detail" in self.need:
            self._emit("course_details", seed.id)
            budget = self.max_detail_pages or float("inf")
            used = 0
            detail_failures: list[str] = []
            for lvl in levels:
                for i, (row, course) in enumerate(built[lvl]):
                    if used >= budget:
                        detail_skipped += 1
                        continue
                    used += 1
                    if not course.url:
                        continue
                    try:
                        dstate = self._fetch_state(course.url, visited)
                    except BlockedError:
                        raise
                    except (HttpStatusError, FetchError, RobotsDisallowed, LayoutChanged) as exc:
                        detail_failures.append(f"course page {course.url}: {exc}")
                        continue
                    detail = course_detail(dstate)
                    if detail:
                        built[lvl][i] = (row, build_course(row, course.degree, cctx, fields, detail))
                    else:
                        detail_failures.append(f"course page {course.url}: no course data in page state")
            for msg in detail_failures[:3]:
                errors.append(msg)
            if len(detail_failures) > 3:
                errors.append(f"... and {len(detail_failures) - 3} more course pages failed")
            for msg in detail_failures:
                logger.warning("%s: %s", seed.id, msg)
        if detail_skipped:
            errors.append(f"{detail_skipped} courses not opened (cap of {self.max_detail_pages} course pages "
                          f"per college): eligibility/details left null for them")
        courses = {lvl: [c for _, c in built[lvl]] for lvl in levels}
        return courses, dup, other_level, detail_skipped


# ====================================================================================== CLI
def _setup_logging(out_dir: Path, verbose: bool) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("scraper.careers360_nirf")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.FileHandler(out_dir / "scraper.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def _print_progress(ev: ProgressEvent) -> None:
    if ev.stage in ("saving", "done", "aborted"):
        print(f"[{ev.stage}] {ev.processed}/{ev.total} processed | matched {ev.matched} failed {ev.failed} "
              f"unmatched {ev.unmatched} ambiguous {ev.ambiguous} pending {ev.pending} | "
              f"courses {ev.courses_extracted}", file=sys.stderr)


LIVE_NOTICE = (
    "Refusing to access careers360.com without --allow-live.\n"
    "robots.txt permits the profile and course pages used here, but Careers360's Terms of Use prohibit "
    "using automated programs to crawl, scrape or extract data from the platform. Review the terms "
    "(https://www.careers360.com/terms-of-use) and, if you have permission or accept the risk, re-run with --allow-live."
)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="careers360_nirf.scraper", description=__doc__.split("\n\n")[0])
    ap.add_argument("--limit", type=int, help="only the first N selected colleges")
    ap.add_argument("--only", action="append", metavar="SEED_ID", help="only this seed id (repeatable)")
    ap.add_argument("--resume", action="store_true", help="skip finished colleges, retry failed ones")
    ap.add_argument("--delay", type=float, default=config.REQUEST_DELAY_SECONDS, help="seconds between requests")
    ap.add_argument("--max-course-details", type=int, default=config.MAX_COURSE_DETAIL_PAGES,
                    help="course pages to open per college for eligibility/details (0 = all)")
    ap.add_argument("--allow-live", action="store_true", help="acknowledge the site's terms and allow network access")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if not args.allow_live:
        print(LIVE_NOTICE, file=sys.stderr)
        return 2
    _setup_logging(config.OUTPUT_DIR, args.verbose)
    try:
        seeds, params = load_seed(), load_parameters()
    except DataFileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    client = PoliteClient(delay=args.delay)
    scraper = Careers360NirfScraper(client, seeds, params, progress=_print_progress,
                                    max_detail_pages=args.max_course_details)
    try:
        report = scraper.run(only=args.only, limit=args.limit, resume=args.resume)
    except DataFileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"match {report['match']} | scrape {report['scrape']} | courses {report['courses_per_level']} | "
          f"report: {scraper.report_file}")
    return 3 if report["run"]["aborted"] else 0


if __name__ == "__main__":
    sys.exit(main())
