import json
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.db.models import Job, JobError


def _utc(d):
    return d.replace(tzinfo=timezone.utc) if d and d.tzinfo is None else d


class JobCreate(BaseModel):
    source: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    params: dict[str, Any] = Field(default_factory=dict)


class JobErrorOut(BaseModel):
    id: int
    timestamp: datetime
    stage: str
    error_code: str
    message: str
    error_type: Optional[str]
    technical_message: Optional[str]
    file: Optional[str]
    function: Optional[str]
    line: Optional[int]
    retryable: bool
    job_continues: bool
    traceback: Optional[str] = None     # only filled when explicitly requested


class JobOut(BaseModel):
    id: str
    source: str
    status: str
    params: dict
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    duration_seconds: Optional[float]
    stage: str
    percent: float
    message: str
    items_total: Optional[int]
    items_done: int
    error_count: int
    stats: Optional[dict]
    has_output: bool
    download_url: Optional[str]
    errors: list[JobErrorOut] = []

    @classmethod
    def from_db(cls, j: Job, errors: Optional[list[JobError]] = None, with_traceback: bool = False) -> "JobOut":
        created, started, completed = _utc(j.created_at), _utc(j.started_at), _utc(j.completed_at)
        dur = None
        if started:
            dur = round(((completed or datetime.now(timezone.utc)) - started).total_seconds(), 1)
        errs = [JobErrorOut(**{c: (_utc(getattr(e, c)) if c == "timestamp" else getattr(e, c))
                               for c in JobErrorOut.model_fields if c != "traceback"},
                            traceback=e.traceback if with_traceback else None) for e in errors or []]
        return cls(id=j.id, source=j.source, status=j.status, params=json.loads(j.params or "{}"),
                   created_at=created, started_at=started, completed_at=completed,
                   duration_seconds=dur, stage=j.stage, percent=j.percent, message=j.message,
                   items_total=j.items_total, items_done=j.items_done, error_count=j.error_count,
                   stats=json.loads(j.stats) if j.stats else None, has_output=bool(j.output_path),
                   download_url=f"/jobs/{j.id}/download" if j.output_path else None, errors=errs)
