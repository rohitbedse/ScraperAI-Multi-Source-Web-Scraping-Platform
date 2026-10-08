"""Platform wrapper around the original SWAYAM scraper (core.py keeps the Playwright logic).

FETCH_LIST    Upcoming/Ongoing tabs + LOAD MORE (core.collect_cards)
PARSE         unique course URLs, limit
FETCH_DETAIL  parallel course pages (core.scrape_course) -> partial/records.jsonl + resume cache
VALIDATE      core.to_row + Pydantic Course          -> partial/validated.jsonl
DEDUPE        by URL                                 -> items.jsonl
SAVE          result.json
"""
import asyncio
import json
import os
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.events import ErrorCode, Stage
from app.scrapers.base import BaseScraper, LayoutChanged, ScraperCancelled, read_jsonl
from app.scrapers.swayam import core

CACHE_MAX_AGE_H = 24      # resume-file entries older than this are scraped again
DEFAULT_WORKERS = max(1, min(10, int(os.environ.get("SWAYAM_WORKERS", "5"))))   # drop to 3 if memory is tight


class SwayamParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(0, ge=0, le=100000, title="Course limit", description="Only scrape the first N courses (0 = all)")
    workers: int = Field(DEFAULT_WORKERS, ge=1, le=10, title="Parallel pages")
    max_clicks: int = Field(120, ge=1, le=500, title="Max LOAD MORE clicks per tab")
    fresh: bool = Field(False, title="Ignore resume cache")
    reuse_cards: bool = Field(False, title="Reuse saved course list", description="Skip the explorer crawl")


class SwayamScraper(BaseScraper):
    id = "swayam"
    name = "SWAYAM Courses"
    description = "Upcoming and ongoing courses from swayam.gov.in (Playwright, parallel pages)."
    ParamsModel = SwayamParams
    max_concurrent = 1                                   # one browser-heavy job at a time
    stages = [Stage.INIT, Stage.FETCH_LIST, Stage.PARSE, Stage.FETCH_DETAIL, Stage.VALIDATE,
              Stage.DEDUPE, Stage.SAVE, Stage.DONE]
    stage_ranges = {
        Stage.INIT: (0, 0), Stage.FETCH_LIST: (0, 15), Stage.PARSE: (15, 15), Stage.FETCH_DETAIL: (15, 80),
        Stage.VALIDATE: (80, 90), Stage.DEDUPE: (90, 95), Stage.SAVE: (95, 99), Stage.DONE: (100, 100),
    }

    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except asyncio.CancelledError:                    # cancelled by SIGTERM, browser already closed
            raise ScraperCancelled() from None

    # ------------------------------------------------------------------ stages
    async def _main(self) -> None:
        from playwright.async_api import async_playwright
        loop, me = asyncio.get_running_loop(), asyncio.current_task()
        self.on_cancel = lambda: loop.call_soon_threadsafe(me.cancel)
        self.check_cancelled()
        os.makedirs(core.STATE_DIR, exist_ok=True)
        records = self.partial_dir / "records.jsonl"

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = None
            try:
                ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
                cards = await self._fetch_list(ctx)
                by_url = self._parse(cards)
                await self._fetch_details(ctx, by_url, records)
            finally:                                      # always release Playwright resources
                try:
                    if ctx:
                        await ctx.close()
                finally:
                    await browser.close()
        self._validate_and_dedupe(by_url, records)

    async def _fetch_list(self, ctx) -> list:
        p = self.params
        self.set_stage(Stage.FETCH_LIST, "Opening the SWAYAM explorer")
        if p.reuse_cards and os.path.exists(core.CARDS_FILE):
            with open(core.CARDS_FILE, encoding="utf-8") as f:
                cards = [core.ExplorerCard(**c) for c in json.load(f)]
            self.log(f"Reusing {len(cards)} saved courses")
            return cards
        tabs = [k for k, _ in core.TABS]
        page = await ctx.new_page()
        try:
            def on_progress(tab: str, n: int, total: int) -> None:
                self.set_percent(tabs.index(tab) / len(tabs))      # real milestone: tabs finished
                self.progress(total, None, f"{tab}: {n} courses found ({total} unique so far)")
            cards = await core.collect_cards(page, p.max_clicks, on_progress=on_progress,
                                             check=self.check_cancelled)
        finally:
            await page.close()
        self.set_percent(1)
        if not cards:
            raise LayoutChanged("No course cards found on the explorer page")
        return cards

    def _parse(self, cards: list) -> dict:
        self.set_stage(Stage.PARSE, "Preparing course URLs")
        if self.params.limit:
            cards = cards[: self.params.limit]
        by_url = {c.url: c for c in cards}
        self.stats["duplicates"] += len(cards) - len(by_url)
        self.log(f"{len(by_url)} unique course URLs")
        return by_url

    async def _fetch_details(self, ctx, by_url: dict, records) -> None:
        p = self.params
        total = len(by_url)
        self.set_stage(Stage.FETCH_DETAIL, f"Scraping {total} courses")
        self.items_total = total
        done_urls: set[str] = set()
        cutoff = time.time() - CACHE_MAX_AGE_H * 3600
        with open(records, "a", encoding="utf-8") as rec_fh:
            if not p.fresh:                                         # streamed resume from cache
                for rec in read_jsonl(Path(core.PROGRESS)):
                    u = rec.get("url")
                    if u in by_url and not rec.get("_error") and u not in done_urls and rec.get("_ts", 0) > cutoff:
                        done_urls.add(u)
                        rec_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n = len(done_urls)
            self.progress(n, total, f"{n} courses resumed from cache" if n else f"Scraped {n}/{total} courses")

            sem = asyncio.Semaphore(p.workers)
            tasks = [asyncio.create_task(core.scrape_course(ctx, c, sem)) for c in by_url.values()
                     if c.url not in done_urls]
            try:
                with open(core.PROGRESS, "a", encoding="utf-8") as cache:
                    for fut in asyncio.as_completed(tasks):
                        rec = await fut
                        self.check_cancelled()
                        card = by_url[rec["url"]]
                        if rec.get("_error"):
                            self._report_course_error(card, rec)
                        else:
                            rec["_ts"] = time.time()
                            cache.write(json.dumps(rec, ensure_ascii=False) + "\n")
                            cache.flush()
                        rec.pop("_tb", None)
                        rec_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        rec_fh.flush()
                        n += 1
                        self.progress(n, total, f"Scraped {n}/{total} courses")
            except BaseException:
                for t in tasks:                                    # stop pending workers
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

    def _report_course_error(self, card, rec: dict) -> None:
        err = rec["_error"]
        if err == "timeout":
            code = ErrorCode.NETWORK_TIMEOUT
        elif "net::ERR" in err:
            code = ErrorCode.HTTP_ERROR
        else:
            code = ErrorCode.SCRAPER_ERROR
        self.report_error(code, error_type=rec.get("_exc") or "Error", technical=err, tb=rec.get("_tb", ""),
                          message=f"Could not read course page: {card.card_name[:60] or card.url}",
                          retryable=True)

    def _validate_and_dedupe(self, by_url: dict, records) -> None:
        validation_errors: list[dict] = []
        status_count: dict[str, int] = {}
        scrape_errors: list[dict] = []

        # ---- VALIDATE
        self.set_stage(Stage.VALIDATE, "Validating courses")
        total = sum(1 for _ in read_jsonl(records))
        for i, rec in enumerate(read_jsonl(records), 1):
            card = by_url[rec["url"]]
            if rec.get("_error"):
                scrape_errors.append({"url": rec["url"], "error": rec["_error"]})
            try:
                row = core.to_row(card, {} if rec.get("_error") else rec)
            except Exception as exc:
                self.handle_error(exc, code=ErrorCode.PARSE_ERROR, stage=Stage.PARSE,
                                  message=f"Could not parse course: {card.card_name[:60] or card.url}")
                self.progress(i, total)
                continue
            good, bad = core.validate([row])
            if bad:
                self.stats["invalid"] += 1
                validation_errors.extend(bad)
            if good:
                self.append_jsonl("validated", good[0].model_dump(mode="json"))
            else:
                self.report_error(ErrorCode.VALIDATION_FAILED, error_type="ValidationError",
                                  technical="; ".join(f"{b['field']}: {b['error']}" for b in bad)[:300],
                                  message=f"A course was dropped because it failed validation ({card.url})",
                                  retryable=False)
            self.progress(i, total, f"Validated {i}/{total} courses")

        # ---- DEDUPE
        self.set_stage(Stage.DEDUPE, "Removing duplicate courses")
        seen: set[str] = set()
        validated = list(read_jsonl(self.partial_dir / "validated.jsonl"))
        for i, row in enumerate(validated, 1):
            if row["url"] in seen:
                self.stats["duplicates"] += 1
            else:
                seen.add(row["url"])
                key = row.get("live_status") or "Unknown"
                status_count[key] = status_count.get(key, 0) + 1
                self.save_item(row)
            self.progress(i, len(validated))
        self.meta.update(live_status_count=status_count, validation_errors=validation_errors,
                         scrape_errors=scrape_errors)
