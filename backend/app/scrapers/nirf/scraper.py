"""Platform wrapper around the NIRF rankings scraper (core.py keeps the Fetcher / discovery / parsing logic).

INIT          options, HTML cache folder, existing dataset (incremental)
FETCH_LIST    newest year (or the chosen one) -> ranking index -> category pages; rank-band / "all" links found
FETCH_DETAIL  rank-band and participating-institution pages
PARSE         every fetched page re-read from the HTML cache -> partial/parsed.jsonl
VALIDATE      duplicate keys, rank order, missing rank/score, parameter ranges -> manifest (audit only, never fails)
DEDUPE        exact duplicate rows dropped; incremental: merged into the existing dataset by dedup key
SAVE          result.json; nirf_{year}.csv + manifest.json beside it; master dataset in STATE_DIR/nirf

The HTML cache is shared between jobs (and with the CLI via NIRF_CACHE_DIR), so "Offline re-parse" can
re-run a parser fix without any network call.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import config
from app.schemas.events import ErrorCode, Stage
from app.scrapers.base import BaseScraper, read_jsonl
from app.scrapers.nirf import core

MAX_CONSECUTIVE_FAILURES = 5          # stop fetching from a site that keeps refusing us
MAX_ISSUES_IN_RESULT = 200            # full list is always in manifest.json


def state_dir() -> Path:
    return config.STATE_DIR / "nirf"


def cache_dir() -> Path:
    return Path(os.environ.get("NIRF_CACHE_DIR") or state_dir() / "html_cache")


class NirfParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: Optional[int] = Field(None, ge=2016, le=2100, title="Ranking year",
                                description="Empty = newest published year")
    categories: list[str] = Field(default_factory=list, max_length=30, title="Categories",
                                  description="Comma separated slugs or names, e.g. Engineering, Pharmacy, "
                                              "Medical (empty = all)")
    no_bands: bool = Field(False, title="Skip rank-band pages", description="Skip 101-150, 151-200 ... pages")
    no_all: bool = Field(False, title="Skip participating lists",
                         description="Skip the 'all participating institutions' pages")
    incremental: bool = Field(False, title="Incremental",
                              description="Merge into the existing NIRF dataset; reports new / updated rows")
    offline: bool = Field(False, title="Offline re-parse",
                          description="Only re-parse the cached HTML; no network requests")
    delay: float = Field(1.0, ge=0.5, le=30, title="Seconds between requests")

    @field_validator("categories")
    @classmethod
    def _clean(cls, cats: list[str]) -> list[str]:
        return [c.strip() for c in cats if c and c.strip()]


class NirfError(Exception):
    """A failure with a platform error code and a clear, user-facing reason."""

    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class NirfScraper(BaseScraper):
    id = "nirf"
    name = "NIRF India Rankings"
    description = ("Official NIRF rankings from nirfindia.org: every category, rank bands and participating "
                   "institutions, with parameter scores, PDF and graph links. Cached HTML, offline re-parse, "
                   "incremental merge.")
    ParamsModel = NirfParams
    max_concurrent = 1                                    # one job at a time: shared HTML cache + master dataset
    stage_ranges = {
        Stage.INIT: (0, 2), Stage.FETCH_LIST: (2, 35), Stage.FETCH_DETAIL: (35, 75), Stage.PARSE: (75, 88),
        Stage.VALIDATE: (88, 92), Stage.DEDUPE: (92, 96), Stage.SAVE: (96, 99), Stage.DONE: (100, 100),
    }

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.year: Optional[int] = None
        self.manifest: dict = {"pages": [], "errors": []}
        self._pages: list[dict] = []                      # fetched pages: category, slug, url, list_type, band
        self._consecutive = 0
        self._fetched = self._failed = 0
        self._existing: dict = {}

    def make_fetcher(self) -> core.Fetcher:
        p = self.params
        return core.Fetcher(p.delay, cache_dir(), p.offline, refresh=not p.offline, sleep=self._interruptible_sleep)

    def handle_error(self, exc, **kw):
        if isinstance(exc, NirfError):
            kw.setdefault("code", exc.code)
            kw.setdefault("message", exc.message)
            kw.setdefault("retryable", exc.code in (ErrorCode.BLOCKED_429, ErrorCode.NETWORK_TIMEOUT,
                                                    ErrorCode.HTTP_ERROR))
        super().handle_error(exc, **kw)

    # ------------------------------------------------------------------ pipeline
    def run(self) -> None:
        p = self.params
        fetcher = self.make_fetcher()
        self.log(f"HTML cache: {fetcher.cache_dir}" + (" (offline: no network)" if p.offline else ""))

        # ---- FETCH_LIST: year, index, category pages
        self.set_stage(Stage.FETCH_LIST, "Finding the ranking year and categories")
        year = p.year or core.discover_year(fetcher)
        if not year:
            raise NirfError(self._fetch_code(fetcher),
                            "Could not find a published NIRF ranking year"
                            + (" in the HTML cache" if p.offline else f" ({fetcher.last_error or 'no categories'})"))
        self.year = year
        self.manifest["year"] = year
        index_url = core.index_url(fetcher.base, year)
        index_html = fetcher.get(index_url)
        if not index_html:
            raise NirfError(self._fetch_code(fetcher),
                            f"Could not load the {year} ranking index: {fetcher.last_error}")
        found = core.discover_categories(index_html, year, fetcher.base)
        if not found:
            raise NirfError(ErrorCode.LAYOUT_CHANGED, f"No category links found on {index_url}")
        cats = core.select_categories(found, p.categories)
        unknown = [c for c in p.categories if not core.select_categories(found, [c])]
        if unknown:
            avail = ", ".join(core.LABELS.get(s, s) for s in found)
            if not cats:
                raise NirfError(ErrorCode.SCRAPER_ERROR,
                                f"None of the requested categories exist for {year}. Available: {avail}")
            self.log(f"Unknown categories ignored: {', '.join(unknown)}. Available: {avail}")
        self.log(f"Ranking year {year}: {len(cats)} categories")
        if p.incremental:
            self._existing, existing = core.load_existing_records(state_dir(), year)
            self.log(f"Incremental: {len(existing)} existing records loaded")

        extra: list[dict] = []
        for i, (slug, url) in enumerate(cats.items(), 1):
            label = core.LABELS.get(slug, slug)
            html = self._fetch(fetcher, url, label)
            if html is not None:
                self._pages.append(dict(category=label, slug=slug, url=url, list_type="ranked", band=None))
                for eurl, (suffix, text) in sorted(core.discover_extra_pages(html, url, slug).items()):
                    lt, band = core.band_label(suffix, text, slug)
                    if (lt == "rank_band" and p.no_bands) or (lt == "participating" and p.no_all):
                        continue
                    extra.append(dict(category=label, slug=slug, url=eurl, list_type=lt, band=band))
            self._live(len(cats) + len(extra))
            self.progress(i, len(cats), f"Category {i}/{len(cats)}: {label}")
            if self._consecutive >= MAX_CONSECUTIVE_FAILURES:
                break
        if not self._pages:
            raise NirfError(self._fetch_code(fetcher), f"No category page could be loaded ({fetcher.last_error})")

        # ---- FETCH_DETAIL: rank bands + participating institutions
        self.set_stage(Stage.FETCH_DETAIL, f"Fetching {len(extra)} rank-band / participating pages")
        self.progress(0, len(extra))
        for i, pg in enumerate(extra, 1):
            if self._consecutive >= MAX_CONSECUTIVE_FAILURES:
                self.log(f"Stopping after {MAX_CONSECUTIVE_FAILURES} failed pages in a row; keeping what was fetched")
                break
            if self._fetch(fetcher, pg["url"], pg["category"]) is not None:
                self._pages.append(pg)
            self._live(len(cats) + len(extra))
            label = pg["band"] or pg["list_type"]
            self.progress(i, len(extra), f"{pg['category']}: {label} ({i}/{len(extra)})")
        self.meta.update(network_requests=fetcher.network_requests, cache_hits=fetcher.cache_hits)

        # ---- PARSE: from the HTML cache, page by page
        self.set_stage(Stage.PARSE, f"Parsing {len(self._pages)} pages")
        reader = core.Fetcher(cache_dir=fetcher.cache_dir, offline=True, base=fetcher.base)
        parsed = 0
        for i, pg in enumerate(self._pages, 1):
            recs, warns = core.parse_ranking_page(reader.get(pg["url"]) or "", url=pg["url"], year=year,
                                                  category=pg["category"], slug=pg["slug"],
                                                  list_type=pg["list_type"], rank_band=pg["band"])
            for r in recs:
                self.append_jsonl("parsed", r)
            parsed += len(recs)
            self.manifest["pages"].append({"category": pg["category"], "url": pg["url"],
                                           "list_type": pg["list_type"], "rank_band": pg["band"],
                                           "rows": len(recs), "warnings": warns})
            if not recs and pg["list_type"] == "ranked":
                self.report_error(ErrorCode.LAYOUT_CHANGED, error_type="NoRankingTable",
                                  technical="; ".join(warns)[:300] or "no rows",
                                  message=f"{pg['category']}: no institutions found on {pg['url']}",
                                  stage=Stage.PARSE, retryable=False)
            self.live_data["records"] = parsed
            self.progress(i, len(self._pages), f"Parsed {pg['category']} {pg['band'] or pg['list_type']}: "
                                               f"{len(recs)} rows")
        if not parsed:
            raise NirfError(ErrorCode.LAYOUT_CHANGED, "No institutions could be parsed from any page")

        # ---- VALIDATE: audit trail only
        self.set_stage(Stage.VALIDATE, "Checking ranks, scores and duplicates")
        records = list(read_jsonl(self.partial_dir / "parsed.jsonl"))
        issues = core.validate(records)
        self.manifest["validation_issues"] = issues
        self.live_data["validation_issues"] = len(issues)
        self.progress(len(records), len(records), f"{len(issues)} validation issues (see manifest.json)")
        if issues:
            self.log(f"{len(issues)} validation issues, e.g. {issues[0]}")

        # ---- DEDUPE (+ incremental merge)
        self.set_stage(Stage.DEDUPE, "Removing duplicate rows")
        unique, seen = [], set()
        for r in records:
            blob = json.dumps(r, sort_keys=True, ensure_ascii=False)
            if blob in seen:                               # identical row twice; same key + other data is kept
                self.stats["duplicates"] += 1
                continue
            seen.add(blob)
            unique.append(r)
        del records, seen
        self.manifest["total_records"] = len(unique)
        if p.incremental:
            new_only, updated = core.filter_new_records(unique, self._existing)
            unique = core.merge_records(self._existing, unique)
            self.manifest["incremental"] = {"new": len(new_only), "updated": len(updated), "total": len(unique)}
            self.log(f"Incremental: {len(new_only)} new, {len(updated)} updated, {len(unique)} total")
        for i, r in enumerate(unique, 1):
            self.save_item(r)
            self.progress(i, len(unique))
        self.stats.update(pages=len(self._pages), pages_failed=self._failed, validation_issues=len(issues))
        self.meta.update(year=year, categories=[core.LABELS.get(s, s) for s in cats],
                         errors=self.manifest["errors"], validation_issues=issues[:MAX_ISSUES_IN_RESULT],
                         **({"incremental": self.manifest["incremental"]} if p.incremental else {}))

    # ------------------------------------------------------------------ helpers
    def _fetch(self, fetcher: core.Fetcher, url: str, label: str) -> Optional[str]:
        self.check_cancelled()
        html = fetcher.get(url)
        if html is not None:
            self._fetched += 1
            self._consecutive = 0
            return html
        self._failed += 1
        self._consecutive += 1
        self.manifest["errors"].append(f"{label}: failed to load {url} ({fetcher.last_error})")
        code = self._fetch_code(fetcher)
        self.report_error(code, error_type="PageUnavailable", technical=f"{url}: {fetcher.last_error}",
                          message=f"{label}: could not load {url.rsplit('/', 1)[-1]}",
                          retryable=not self.params.offline and code != ErrorCode.BLOCKED_403)
        return None

    def _fetch_code(self, fetcher: core.Fetcher) -> ErrorCode:
        if self.params.offline:
            return ErrorCode.SCRAPER_ERROR
        if fetcher.last_status == 403:
            return ErrorCode.BLOCKED_403
        if fetcher.last_status == 429:
            return ErrorCode.BLOCKED_429
        if (fetcher.last_error or "").startswith("timeout"):
            return ErrorCode.NETWORK_TIMEOUT
        return ErrorCode.HTTP_ERROR

    def _live(self, total: int) -> None:
        self.live_data.update(processed=self._fetched + self._failed, total=total, failed=self._failed,
                              pending=max(0, total - self._fetched - self._failed))

    def _interruptible_sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while True:
            self.check_cancelled()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.1, left))

    # ------------------------------------------------------------------ outputs
    def commit(self) -> None:
        """After result.json exists: CSV + manifest beside it, and the master dataset for incremental runs."""
        if self.year is None:
            return
        records = list(read_jsonl(self.partial_path))
        out = self.output_path.parent
        core.write_csv(records, out / f"nirf_{self.year}.csv")
        core.write_manifest(self.manifest, out)
        p = self.params
        complete = not (p.categories or p.no_bands or p.no_all or self._failed)
        if not (p.incremental or complete):            # a subset must not replace the master dataset
            self.log("Master dataset not updated (filtered or incomplete run; use Incremental to merge)")
            return
        master = state_dir()
        master.mkdir(parents=True, exist_ok=True)
        tmp = master / f"nirf_{self.year}.json.tmp"
        tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, core.output_json_path(master, self.year))
        for name in (f"nirf_{self.year}.csv", "manifest.json"):
            shutil.copyfile(out / name, master / name)
