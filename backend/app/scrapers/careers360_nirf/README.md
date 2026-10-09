# Careers360 NIRF scraper

Collects college, course, ranking and study-destination data for the 100 NIRF-ranked institutions
from Careers360 profile pages.

## Read this first: terms of use

`robots.txt` allows the profile/course pages used here (it disallows `/search/`, `*/api/*` and
`*/ajax*`, which this scraper never calls). **Careers360's Terms of Use, however, prohibit using
"automated programs ... to crawl, scrape or extract" data** (https://www.careers360.com/terms-of-use).
The CLI therefore refuses to touch the network unless you pass `--allow-live`. Get permission or
accept the risk before using it. If the site answers 403/429 or a CAPTCHA, the scraper stops that
college, logs it, and does not retry or work around it.

## Run (from `backend/`)

```
python -m app.scrapers.careers360_nirf.scraper --allow-live --limit 3
python -m app.scrapers.careers360_nirf.scraper --allow-live --only IR-O-U-0456
python -m app.scrapers.careers360_nirf.scraper --allow-live --resume
```

Options: `--limit N`, `--only SEED_ID` (repeatable), `--resume` (skip finished colleges, retry failed),
`--delay SECONDS` (default 2), `--max-course-details N` (course pages opened per college for
eligibility/details/admission text; default 30, 0 = all). Set `CAREERS360_CONTACT` to put a contact
in the User-Agent. Tests: `python -m pytest app/scrapers/careers360_nirf/tests` (offline; live test needs `CAREERS360_LIVE=1`).

## Inputs (read at runtime, never hardcoded)

| File | Contents |
|---|---|
| `data/nirf_seed.json` | 100 records `{id, name, city, state, location, score, rank}`; ties (27, 64) preserved. |
| `data/parameters.yaml` | `college_parameters`, `course_parameters` (`degree_levels` + `fields`), `study_destination`, `ranking_category`. |

Both are validated with Pydantic on load; a missing, empty or malformed file stops the run with a clear message.
The set of fields in the output comes from the YAML. A key already known to the extractor
(`extractor.COLLEGE` / `RANKING` / `COURSE`) is extracted automatically; for a new college- or ranking-level
value that sits in the profile page state, add `path: dotted.path.in.page.state` to its YAML entry and no code
change is needed. A key with neither is output as null and listed under `unbound_parameters` in the report.

## How it works

1. **Match** (`matcher.py`): the site search is off limits, so candidates come from the public profile sitemap
   (cached 7 days in `output/cache/`). Names are normalized and scored; a profile is accepted only when clearly best
   and its page states the seed's state. Close ties between campuses are resolved only if exactly one candidate's own
   profile states the seed's city and state; otherwise the record is `ambiguous` with its candidates and no data.
2. **Discover** (`page_discovery.py`): sub-pages come from the college's own navigation; only the pages needed for the
   YAML parameters are fetched. Pages embed their data as `window.INITIAL_STATE` JSON, which is parsed (no browser needed).
3. **Extract** (`extractor.py`): missing values are `null` and listed in `missing_fields`. `nirf_ranking` is the seed rank;
   QS/THE only if the page lists them; `study_destination` is `true` only when the page's campus address names the country.
4. **Courses**: grouped UG / PG / PhD by degree page. Fees, duration and exam names are normalized, with the original
   text in `raw`. Duplicates (same id, or same level + name + duration + mode) are skipped and counted.

## Output (`output/`)

`colleges_data.json` (list of validated records), `scrape_report.json`, `checkpoint.json`, `scraper.log`, `cache/`.
Writes are atomic (temp file + replace). Existing records are kept when you run a subset.

## Layout

`scraper.py` orchestration + CLI + progress callback (`ProgressEvent`) · `http_client.py` robots, rate limit, retries ·
`matcher.py` · `page_discovery.py` · `extractor.py` · `schemas.py` · `checkpoint.py` · `storage.py` · `report.py` ·
`config.py` · `fixtures/` (trimmed copies of real page state) · `tests/`.

## Known limitations

- `research_areas` is never published by Careers360: always null.
- QS / THE rankings: Careers360 profile data only carries NIRF and Careers360 rankings for the pages seen; null otherwise.
- Eligibility, course details and admission text need one page per course, capped per college (`course_details_skipped`).
- Diploma, certificate and combined/dual degrees are not UG/PG/PhD and are skipped (`courses_skipped_other_level`).
- Degree-label to level mapping is rule-based (`config.DEGREE_LEVEL_RULES`); unknown labels are skipped, not guessed.
- Multi-campus brands whose profiles cannot be told apart by city and state stay `ambiguous`.
