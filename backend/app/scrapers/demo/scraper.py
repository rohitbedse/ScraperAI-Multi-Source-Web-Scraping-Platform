"""Offline demo scraper: exercises every platform feature without touching the network.

Pipeline (data flows through partial/*.jsonl, like the real scrapers):
  FETCH_LIST -> FETCH_DETAIL (raw) -> PARSE (parsed) -> VALIDATE (validated) -> DEDUPE (items) -> SAVE
"""
import time
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas.events import ErrorCode, Stage
from app.scrapers.base import BaseScraper, read_jsonl


class DemoItem(BaseModel):
    id: int
    title: str = Field(min_length=1)


class DemoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: int = Field(20, ge=1, le=1000, title="Items", description="How many fake items to produce")
    delay_ms: int = Field(100, ge=0, le=5000, title="Delay per item (ms)")
    soft_errors: int = Field(0, ge=0, le=50, title="Soft errors", description="Recoverable errors to emit")
    fail_at: Optional[int] = Field(None, ge=1, title="Fail at item", description="Raise an exception at this item")
    crash_at: Optional[int] = Field(None, ge=1, title="Hard crash at item", description="Kill the process without any error event")
    fail_save: bool = Field(False, title="Fail while saving", description="Simulate an unwritable output")


class DemoScraper(BaseScraper):
    id = "demo"
    name = "Demo (offline)"
    description = "Generates fake items to test the platform: progress, errors, cancel, download."
    ParamsModel = DemoParams

    # ------------------------------------------------------------------ pipeline
    def run(self) -> None:
        p = self.params

        # ---- FETCH_LIST
        self.set_stage(Stage.FETCH_LIST, "Listing items")
        time.sleep(0.2)
        ids = list(range(1, p.items + 1))
        self.log(f"Found {len(ids)} items")

        # ---- FETCH_DETAIL -> partial/raw.jsonl
        self.set_stage(Stage.FETCH_DETAIL, "Fetching items")
        for i in ids:
            time.sleep(p.delay_ms / 1000)
            if p.crash_at and i >= p.crash_at:
                import os, sys
                print("demo: simulated hard crash", file=sys.stderr, flush=True)
                os._exit(7)
            if p.fail_at and i >= p.fail_at:
                raise RuntimeError(f"demo crash at item {i}")
            if i <= p.soft_errors:
                self.handle_error(TimeoutError(f"item {i} timed out"), code=ErrorCode.NETWORK_TIMEOUT)
            self.append_jsonl("raw", {"id": i, "title": f"Demo item {i}"})
            self.progress(i, p.items, f"Fetched item {i}/{p.items}")

        # ---- PARSE: raw -> parsed.jsonl
        raw = list(read_jsonl(self.partial_dir / "raw.jsonl"))
        self.set_stage(Stage.PARSE, "Parsing items")
        for i, rec in enumerate(raw, 1):
            self.append_jsonl("parsed", {"id": rec["id"], "title": str(rec["title"]).strip()})
            self.progress(i, len(raw))
        del raw

        # ---- VALIDATE: parsed -> validated.jsonl
        parsed = list(read_jsonl(self.partial_dir / "parsed.jsonl"))
        self.set_stage(Stage.VALIDATE, "Validating")
        for i, rec in enumerate(parsed, 1):
            try:
                DemoItem.model_validate(rec)
                self.append_jsonl("validated", rec)
            except ValidationError as exc:
                self.stats["invalid"] += 1
                self.handle_error(exc, code=ErrorCode.VALIDATION_FAILED,
                                  message=f"Item {rec.get('id')} failed validation")
            self.progress(i, len(parsed))
        del parsed

        # ---- DEDUPE: validated -> items.jsonl
        validated = list(read_jsonl(self.partial_dir / "validated.jsonl"))
        self.set_stage(Stage.DEDUPE, "Removing duplicates")
        seen: set[int] = set()
        for i, rec in enumerate(validated, 1):
            if rec["id"] in seen:
                self.stats["duplicates"] += 1
            else:
                seen.add(rec["id"])
                self.save_item(rec)
            self.progress(i, len(validated))
        del validated

        if p.fail_save:                       # point the output at a path that cannot be written
            self.output_path = self.job_dir / "partial" / "items.jsonl" / "result.json"
