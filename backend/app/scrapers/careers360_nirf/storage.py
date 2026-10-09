"""Atomic JSON writes and loading of previously saved records. Only touches this scraper's output folder."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError

from app.scrapers.careers360_nirf.schemas import CollegeRecord, DataFileError


def atomic_write_json(path: Path, data: Any) -> None:
    """Write to a temp file in the same folder, then replace: readers never see a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def save_records(path: Path, records: Iterable[CollegeRecord]) -> None:
    atomic_write_json(path, [r.model_dump(mode="json") for r in records])


def load_records(path: Path) -> dict[str, CollegeRecord]:
    """Existing records keyed by seed id ({} if none). A corrupt file is an error, never silently discarded."""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "[]")
        return {r.seed.id: r for r in (CollegeRecord.model_validate(x) for x in data)}
    except (json.JSONDecodeError, ValidationError, AttributeError, TypeError) as exc:
        raise DataFileError(f"{path}: existing output is unreadable ({exc}). "
                            f"Move or delete it to start fresh.") from exc
