"""Error taxonomy: map any exception to an ErrorCode + friendly / technical messages."""
import os
import traceback
from typing import Optional

from app.schemas.events import ErrorCode, Stage

FRIENDLY = {
    ErrorCode.NETWORK_TIMEOUT: "The site took too long to respond.",
    ErrorCode.HTTP_ERROR: "Request failed. The site returned an error or could not be reached.",
    ErrorCode.BLOCKED_403: "The site refused access (HTTP 403). The scraper may be blocked.",
    ErrorCode.BLOCKED_429: "The site is rate limiting requests (too many requests). Try again later.",
    ErrorCode.PARSE_ERROR: "The data returned by the site could not be understood.",
    ErrorCode.LAYOUT_CHANGED: "The site layout looks different than expected.",
    ErrorCode.VALIDATION_FAILED: "Some scraped records failed validation.",
    ErrorCode.SAVE_ERROR: "The results could not be saved.",
    ErrorCode.SCRAPER_ERROR: "The scraper hit an internal error.",
    ErrorCode.CANCELLED: "The job was cancelled.",
    ErrorCode.PROCESS_EXITED_UNEXPECTEDLY: "The scraper process stopped unexpectedly.",
    ErrorCode.UNKNOWN_ERROR: "An unexpected error occurred.",
}
RETRYABLE = {ErrorCode.NETWORK_TIMEOUT, ErrorCode.HTTP_ERROR, ErrorCode.BLOCKED_429,
             ErrorCode.SCRAPER_ERROR, ErrorCode.PROCESS_EXITED_UNEXPECTEDLY, ErrorCode.UNKNOWN_ERROR}


def _status(exc: BaseException) -> Optional[int]:
    return getattr(getattr(exc, "response", None), "status_code", None)


def classify(exc: BaseException, stage: Optional[Stage] = None) -> ErrorCode:
    """Name based, so this module needs no scraping libraries."""
    names = {c.__name__ for c in type(exc).__mro__}
    status = _status(exc)
    if status == 403:
        return ErrorCode.BLOCKED_403
    if status == 429:
        return ErrorCode.BLOCKED_429
    if status or "HTTPError" in names:
        return ErrorCode.HTTP_ERROR
    if "ScraperParseError" in names:
        return ErrorCode.PARSE_ERROR
    if "LayoutChanged" in names:
        return ErrorCode.LAYOUT_CHANGED
    if names & {"JSONDecodeError", "ParserRejectedMarkup", "ParseError"}:
        return ErrorCode.PARSE_ERROR
    if names & {"Timeout", "TimeoutError", "TimeoutException"}:
        return ErrorCode.NETWORK_TIMEOUT
    if names & {"ConnectionError", "RequestException", "ConnectError", "gaierror"}:
        return ErrorCode.HTTP_ERROR
    if "ValidationError" in names:
        return ErrorCode.VALIDATION_FAILED
    if "OSError" in names:
        return ErrorCode.SAVE_ERROR
    if names & {"KeyError", "AttributeError", "IndexError", "TypeError", "ValueError"}:
        return ErrorCode.PARSE_ERROR if stage == Stage.PARSE else ErrorCode.SCRAPER_ERROR
    # any other exception raised by scraper code is a scraper bug; UNKNOWN is for non-Exception failures
    return ErrorCode.SCRAPER_ERROR if isinstance(exc, Exception) else ErrorCode.UNKNOWN_ERROR


def is_retryable(code: ErrorCode, exc: Optional[BaseException] = None) -> bool:
    status = _status(exc) if exc else None
    if code == ErrorCode.HTTP_ERROR and status and 400 <= status < 500:
        return False
    return code in RETRYABLE


def technical_message(exc: BaseException) -> str:
    status = _status(exc)
    if status:
        reason = getattr(exc.response, "reason", "") or ""
        return f"HTTP {status} {reason}".strip()
    return f"{type(exc).__name__}: {exc}"[:500]


def origin(exc: BaseException) -> tuple[Optional[str], Optional[str], Optional[int]]:
    """(file, function, line) of the innermost frame, preferring our own code."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return None, None, None
    own = [f for f in frames if os.sep + "app" + os.sep in f.filename]
    f = (own or frames)[-1]
    return os.path.basename(f.filename), f.name, f.lineno
