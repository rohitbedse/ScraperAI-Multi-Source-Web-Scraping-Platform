# Scraper Platform

A small web app for running web scrapers and watching them work. Pick a source, set its options, start a job, and follow live progress. When it finishes, download the result as JSON.

## What it does

- Runs several scrapers (sources) from one place: **NIRF India Rankings**, **Careers360 NIRF**, **Mindler**, **Swayam**, and an offline **Demo**.
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
  app/scrapers/   one folder per source (nirf, careers360_nirf, demo, mindler, swayam)
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

## NIRF India Rankings

Official rankings from nirfindia.org (`backend/app/scrapers/nirf/`). Finds the newest year, every category, and the rank-band and participating-institution pages from the site's own links. Each record has the parameter scores, PDF and graph links, and the `source_url` it came from.

Options: ranking year, categories (e.g. `Engineering, Pharmacy, Medical`), skip rank bands, skip participating lists, incremental, offline re-parse, delay between requests.

- Raw HTML is cached in `DATA_DIR/state/nirf/html_cache`, or in `NIRF_CACHE_DIR` if set. **Offline re-parse** reads only that cache, so you can test parser fixes without network calls.
- Each job writes `result.json`, `nirf_{year}.csv` (one column per parameter score) and `manifest.json` (per-page counts, fetch errors, validation issues).
- The master dataset `DATA_DIR/state/nirf/nirf_{year}.json` is updated by complete runs and by **Incremental** runs. Incremental runs merge by `(year, category, list_type, rank_band, institute id or name|city)`.
- Validation problems (duplicate keys, rank order, missing rank or score, parameter scores out of range) are recorded in the manifest and never fail the job.

The same scraper also runs as a CLI (from `backend/`):

```
python -m app.scrapers.nirf.core --cache-dir html_cache -v              # full run
python -m app.scrapers.nirf.core --incremental --cache-dir html_cache   # daily cron
python -m app.scrapers.nirf.core --categories Engineering Pharmacy Medical
python -m app.scrapers.nirf.core --offline --cache-dir html_cache       # re-parse only
```

## Adding a scraper

Subclass `BaseScraper` in `backend/app/scrapers/<name>/scraper.py`, define its options with a Pydantic model, and implement `run()`. The `demo` scraper is the simplest example.
