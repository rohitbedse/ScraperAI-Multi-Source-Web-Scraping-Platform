"""The one event model shared by scrapers, runner, DB, SSE and the frontend."""
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Stage(str, Enum):
    INIT = "INIT"
    FETCH_LIST = "FETCH_LIST"
    FETCH_DETAIL = "FETCH_DETAIL"
    PARSE = "PARSE"
    VALIDATE = "VALIDATE"
    DEDUPE = "DEDUPE"
    SAVE = "SAVE"
    DONE = "DONE"
    FAILED = "FAILED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"


# normal pipeline order (FAILED / PARTIALLY_COMPLETED are terminal side-exits)
STAGE_ORDER = [Stage.INIT, Stage.FETCH_LIST, Stage.FETCH_DETAIL, Stage.PARSE,
               Stage.VALIDATE, Stage.DEDUPE, Stage.SAVE, Stage.DONE]
# overall percent range each stage occupies
STAGE_RANGE = {
    Stage.INIT: (0, 2), Stage.FETCH_LIST: (2, 20), Stage.FETCH_DETAIL: (20, 85),
    Stage.PARSE: (85, 90), Stage.VALIDATE: (90, 94), Stage.DEDUPE: (94, 96),
    Stage.SAVE: (96, 99), Stage.DONE: (100, 100),
}


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    cancelling = "cancelling"
    completed = "completed"
    partially_completed = "partially_completed"
    failed = "failed"
    cancelled = "cancelled"


TERMINAL_STATUSES = {JobStatus.completed, JobStatus.partially_completed,
                     JobStatus.failed, JobStatus.cancelled}


class EventType(str, Enum):
    status = "status"       # job state change (written by the runner)
    stage = "stage"
    progress = "progress"
    log = "log"
    error = "error"
    done = "done"


class ErrorCode(str, Enum):
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    HTTP_ERROR = "HTTP_ERROR"
    BLOCKED_403 = "BLOCKED_403"
    BLOCKED_429 = "BLOCKED_429"
    PARSE_ERROR = "PARSE_ERROR"
    LAYOUT_CHANGED = "LAYOUT_CHANGED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    SAVE_ERROR = "SAVE_ERROR"
    SCRAPER_ERROR = "SCRAPER_ERROR"
    CANCELLED = "CANCELLED"
    PROCESS_EXITED_UNEXPECTEDLY = "PROCESS_EXITED_UNEXPECTEDLY"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Event(BaseModel):
    job_id: str
    source: str
    stage: Stage
    percent: float = 0
    items_done: int = 0
    items_total: Optional[int] = None
    message: str = ""
    timestamp: datetime = Field(default_factory=utcnow)
    event_type: EventType
    # error events only
    error_code: Optional[ErrorCode] = None
    error_type: Optional[str] = None         # exception class name
    technical_message: Optional[str] = None  # raw exception text, e.g. "HTTP 429 Too Many Requests"
    file: Optional[str] = None
    function: Optional[str] = None
    line: Optional[int] = None
    retryable: Optional[bool] = None
    job_continues: Optional[bool] = None
    # optional scraper-specific live numbers (e.g. matched / unmatched counts); the UI renders them generically
    data: Optional[dict] = None
