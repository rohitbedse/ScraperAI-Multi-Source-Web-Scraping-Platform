# Scraper Platform

A small web app for running web scrapers and watching them work. Pick a source, set its options, start a job, and follow live progress. When it finishes, download the result as JSON.

## What it does

- Runs several scrapers (sources) from one place: **Mindler**, **Swayam**, and an offline **Demo**.
- Runs each job in its own process, with a limit on how many run at once.
- Streams live progress and errors to the UI.
- Lets you cancel, re-run and download jobs.
- Keeps partial results when a job fails partway.

## How a scraper runs

Every scraper follows the same stages:

`Fetch list → Fetch detail → Parse → Validate → Dedupe → Save`

Data moves between stages through files in `partial/`. Nothing big is held in memory. The final `result.json` is written at the end.

## Project layout

```
backend/    FastAPI + SQLite API, job manager, scrapers
  app/scrapers/   one folder per source (demo, mindler, swayam)
  tests/          pytest suite
frontend/   React + Vite + TypeScript UI
```

## Run it

**Backend** (from `backend/`):

```
pip install -r requirements.txt
uvicorn app.main:app --reload
```

**Frontend** (from `frontend/`):

```
npm install
npm run dev
```

Open http://localhost:5173. The API runs on port 8000.

**Tests** (from `backend/`): `python -m pytest tests`

## API in brief

| Endpoint | Purpose |
|---|---|
| `GET /sources` | List scrapers and their options |
| `POST /jobs` | Start a job |
| `GET /jobs`, `GET /jobs/{id}` | List jobs / job details |
| `GET /jobs/{id}/events` | Live progress (SSE) |
| `POST /jobs/{id}/cancel` | Cancel a job |
| `POST /jobs/{id}/rerun` | Run it again |
| `GET /jobs/{id}/download` | Download the result JSON |

## Settings

Set these as environment variables (all optional):

`DATA_DIR`, `MAX_CONCURRENT_JOBS` (default 2), `OUTPUT_TTL_DAYS` (7), `API_KEY`, `CORS_ORIGINS`, `ENABLE_DEMO` (on).

## Adding a scraper

Subclass `BaseScraper` in `backend/app/scrapers/<name>/scraper.py`, define its options with a Pydantic model, and implement `run()`. The `demo` scraper is the simplest example.
