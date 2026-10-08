"""Platform wrapper around the original Mindler scraper (core.py holds the unchanged logic).

Pipeline (each stage is real work; data flows through partial/*.jsonl, not RAM):
  FETCH_LIST    careerDomainNameList
  FETCH_DETAIL  careerDomainDetails per domain  -> partial/raw.jsonl
  PARSE         core.parse_career_details       -> partial/parsed.jsonl
  VALIDATE      structural check                -> partial/validated.jsonl
  DEDUPE        core.deduplicate_subcareers + previous-data preservation -> items.jsonl
  SAVE          job output; the master library is updated only after the job output is written
"""
import json
import os
import time

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas.events import ErrorCode, Stage
from app.scrapers.base import BaseScraper, LayoutChanged, ScraperParseError, read_jsonl
from app.scrapers.mindler import core

MAX_CONSECUTIVE_FAILURES = 5     # stop hammering a site that keeps refusing us


class MindlerParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(0, ge=0, le=500, title="Domain limit", description="Only fetch the first N career domains (0 = all)")


class DomainRecord(BaseModel):
    subject_id: str = Field(min_length=1)
    subject_title: str = Field(min_length=1)
    subcareers: list[dict]


class MindlerScraper(BaseScraper):
    id = "mindler"
    name = "Mindler Career Library"
    description = "Career domains and sub-careers from careerlibrary.mindler.com (API based)."
    ParamsModel = MindlerParams
    max_concurrent = 2
    stage_ranges = {
        Stage.INIT: (0, 2), Stage.FETCH_LIST: (2, 8), Stage.FETCH_DETAIL: (8, 70), Stage.PARSE: (70, 82),
        Stage.VALIDATE: (82, 88), Stage.DEDUPE: (88, 94), Stage.SAVE: (94, 99), Stage.DONE: (100, 100),
    }

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.library: dict = {}          # master data from earlier runs (read-only until commit())
        self.new_records: dict = {}      # valid records produced by this job

    # ------------------------------------------------------------------ pipeline
    def run(self) -> None:
        session = requests.Session()
        session.headers.update({"User-Agent": core.UA})
        self.library = core.load_existing_library()

        # ---- FETCH_LIST
        self.set_stage(Stage.FETCH_LIST, "Fetching career domains")
        try:
            domains = core.fetch_domain_list(session)
        except (AttributeError, KeyError, TypeError) as exc:
            raise ScraperParseError(f"Unexpected domain list response: {exc!r}") from exc
        if not domains:
            raise LayoutChanged("Domain list came back empty (API layout may have changed)")
        if self.params.limit:
            domains = domains[: self.params.limit]
        total = len(domains)
        self.log(f"Found {total} career domains")

        # ---- FETCH_DETAIL
        self.set_stage(Stage.FETCH_DETAIL, f"Fetching {total} career domains")
        fetched, consecutive = 0, 0
        for i, domain in enumerate(domains, 1):
            self.progress(i - 1, total, f"Fetching career domain {i}/{total}: {domain['career_domain_name']}")
            try:
                resp = core.fetch_domain_details(session, domain["tagline"])
                self.append_jsonl("raw", {"domain": domain, "response": resp})
                fetched += 1
                consecutive = 0
            except Exception as exc:
                consecutive += 1
                if not fetched and (consecutive >= MAX_CONSECUTIVE_FAILURES or i == total):
                    raise                                   # nothing worked at all: fail the job
                self.handle_error(exc, message=f"Could not fetch '{domain['career_domain_name']}'")
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    self.log("Stopping early after repeated request failures; saving what was fetched")
                    break
            time.sleep(0.3)  # be polite
        self.progress(fetched, total, f"Fetched {fetched}/{total} career domains")

        # ---- PARSE
        raw = list(read_jsonl(self.partial_dir / "raw.jsonl"))
        self.set_stage(Stage.PARSE, "Parsing career details")
        for i, item in enumerate(raw, 1):
            domain = item["domain"]
            try:
                try:
                    data_list = item["response"].get("data", [])
                    career_details = data_list[0].get("_source", {}).get("career_details", []) if data_list else []
                    if not isinstance(career_details, list):
                        raise TypeError("career_details is not a list")
                    subcareers = core.parse_career_details(career_details)
                except (AttributeError, KeyError, TypeError, IndexError) as exc:
                    raise ScraperParseError(
                        f"Unexpected response for '{domain['career_domain_name']}': {exc!r}") from exc
                if not data_list:
                    self.log(f"No data returned for {domain['career_domain_name']}")
                self.append_jsonl("parsed", {"subject_id": core.subject_id_for(domain["tagline"]),
                                             "subject_title": domain["career_domain_name"],
                                             "description": domain["description"], "image": domain["image"],
                                             "subcareers": subcareers})
            except ScraperParseError as exc:
                self.handle_error(exc, message=f"Could not read the data for '{domain['career_domain_name']}'")
            self.progress(i, len(raw), f"Parsed domain {i}/{len(raw)}")
        del raw

        # ---- VALIDATE
        parsed = list(read_jsonl(self.partial_dir / "parsed.jsonl"))
        self.set_stage(Stage.VALIDATE, "Validating records")
        for i, rec in enumerate(parsed, 1):
            try:
                DomainRecord.model_validate(rec)
                self.append_jsonl("validated", rec)
            except ValidationError as exc:
                self.stats["invalid"] += 1
                self.handle_error(exc, code=ErrorCode.VALIDATION_FAILED,
                                  message=f"Record '{rec.get('subject_title')}' failed validation")
            self.progress(i, len(parsed))
        del parsed

        # ---- DEDUPE (+ previous-data preservation)
        validated = list(read_jsonl(self.partial_dir / "validated.jsonl"))
        self.set_stage(Stage.DEDUPE, "Removing duplicates")
        saved_ids: set[str] = set()
        for i, rec in enumerate(validated, 1):
            before = len(rec["subcareers"])
            rec["subcareers"] = core.deduplicate_subcareers(rec["subcareers"])
            self.stats["duplicates"] += before - len(rec["subcareers"])
            previous = self.library.get(rec["subject_id"], {})
            if not rec["subcareers"] and previous.get("subcareers"):
                rec["subcareers"] = previous["subcareers"]      # nothing came back: keep the old data
            self.save_item(rec)
            self.new_records[rec["subject_id"]] = rec
            saved_ids.add(rec["subject_id"])
            self.progress(i, len(validated))
        del validated

        # domains that failed this time: keep the valid previous record, else an error placeholder
        for domain in domains:
            sid = core.subject_id_for(domain["tagline"])
            if sid in saved_ids:
                continue
            previous = self.library.get(sid)
            if previous and not previous.get("error"):
                self.save_item(previous)
                self.new_records[sid] = previous
            else:
                self.save_item({"subject_id": sid, "subject_title": domain["career_domain_name"],
                                "description": domain["description"], "image": domain["image"],
                                "subcareers": [], "error": "not fetched in this run"})
            saved_ids.add(sid)
        if not self.params.limit:                              # known from earlier runs, not listed now
            for sid, previous in self.library.items():
                if sid not in saved_ids:
                    self.save_item(previous)
                    self.new_records[sid] = previous
        self.meta["total_subcareers"] = sum(len(r["subcareers"]) for r in self.new_records.values())

    # ------------------------------------------------------------------ master library
    def commit(self) -> None:
        """Runs only after result.json is written, so a failed job never corrupts the master."""
        merged = {**self.library, **self.new_records}
        tmp = core.OUTPUT_FILE + ".tmp"
        os.makedirs(os.path.dirname(core.OUTPUT_FILE) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(merged.values()), f, ensure_ascii=False)
        os.replace(tmp, core.OUTPUT_FILE)
