"""Resume state: which colleges are finished and which failed (failed ones are retried)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.scrapers.careers360_nirf import config
from app.scrapers.careers360_nirf.schemas import DataFileError
from app.scrapers.careers360_nirf.storage import atomic_write_json

FINISHED = {"scraped", "partial", "unmatched", "ambiguous"}   # final outcomes; "failed" is retried


class Checkpoint:
    def __init__(self, path: Path = config.CHECKPOINT_FILE):
        self.path = Path(path)
        self.entries: dict[str, dict] = {}

    @classmethod
    def load(cls, path: Path = config.CHECKPOINT_FILE) -> "Checkpoint":
        cp = cls(path)
        if cp.path.is_file():
            try:
                cp.entries = json.loads(cp.path.read_text(encoding="utf-8")).get("colleges", {})
            except (json.JSONDecodeError, AttributeError) as exc:
                raise DataFileError(f"{cp.path}: checkpoint unreadable ({exc}). Delete it to start fresh.") from exc
        return cp

    def is_done(self, seed_id: str) -> bool:
        return self.entries.get(seed_id, {}).get("status") in FINISHED

    def failed_ids(self) -> list[str]:
        return [k for k, v in self.entries.items() if v.get("status") == "failed"]

    def mark(self, seed_id: str, status: str, error: Optional[str] = None) -> None:
        prev = self.entries.get(seed_id, {})
        self.entries[seed_id] = {"status": status, "attempts": prev.get("attempts", 0) + 1, "error": error,
                                 "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def save(self) -> None:
        atomic_write_json(self.path, {"version": 1, "colleges": self.entries})
