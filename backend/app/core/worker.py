"""Subprocess entry point:  python -m app.core.worker <job_id> <source> [params_json]

stdout carries ONLY event JSON lines (the parent persists them); everything else goes to stderr
(the job log). This process never touches SQLite.
"""
import json
import signal
import sys
import traceback
from datetime import datetime, timezone


def _emit_fatal(out, job_id: str, source: str, exc: BaseException) -> None:
    """Crash before/outside the scraper's own handling (bad import, bad params...)."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(tb, file=sys.stderr, flush=True)
    out.write(json.dumps({
        "job_id": job_id, "source": source, "stage": "INIT", "percent": 0, "items_done": 0,
        "items_total": None, "message": "The scraper could not start.",
        "timestamp": datetime.now(timezone.utc).isoformat(), "event_type": "error",
        "error_code": "SCRAPER_ERROR", "error_type": type(exc).__name__,
        "technical_message": f"{type(exc).__name__}: {exc}"[:600], "retryable": False,
        "job_continues": False, "traceback": tb}) + "\n")
    out.flush()


def main() -> int:
    job_id, source = sys.argv[1], sys.argv[2]
    params = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    out, sys.stdout = sys.stdout, sys.stderr
    for s in (out, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        from app import config
        config.ensure_dirs()
        from app.scrapers.registry import get_scraper_class
        scraper = get_scraper_class(source)(job_id, params, out=out)
    except BaseException as exc:  # noqa: BLE001
        _emit_fatal(out, job_id, source, exc)
        return 1

    def on_signal(*_):                      # graceful cancel: the scraper cleans up and exits
        scraper.request_cancel()

    signal.signal(signal.SIGTERM, on_signal)
    if hasattr(signal, "SIGBREAK"):         # Windows: parent sends CTRL_BREAK_EVENT
        signal.signal(signal.SIGBREAK, on_signal)
    return scraper.execute()


if __name__ == "__main__":
    sys.exit(main())
