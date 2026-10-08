"""Runtime settings, all overridable through environment variables."""
import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BACKEND_DIR / "data")).resolve()
JOBS_DIR = DATA_DIR / "jobs"            # data/jobs/<job_id>/{partial,output,logs}
STATE_DIR = DATA_DIR / "state"          # master datasets + resume caches shared between jobs
DB_URL = f"sqlite:///{(DATA_DIR / 'platform.db').as_posix()}"

MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))
CANCEL_GRACE_SECONDS = float(os.environ.get("CANCEL_GRACE_SECONDS", "10"))  # SIGTERM -> force kill
OUTPUT_TTL_DAYS = float(os.environ.get("OUTPUT_TTL_DAYS", "7"))
API_KEY = os.environ.get("API_KEY") or None     # optional; unset = no auth
CORS_ORIGINS = [o.strip() for o in os.environ.get(
    "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip()]
ENABLE_DEMO = os.environ.get("ENABLE_DEMO", "1") not in ("0", "false", "")

# the legacy scraper modules keep their resume / previous-data files here
os.environ.setdefault("SCRAPER_STATE_DIR", str(STATE_DIR))


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def ensure_job_dirs(job_id: str) -> Path:
    d = job_dir(job_id)
    for sub in ("partial", "output", "logs"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def ensure_dirs() -> None:
    for d in (JOBS_DIR, STATE_DIR):
        d.mkdir(parents=True, exist_ok=True)
