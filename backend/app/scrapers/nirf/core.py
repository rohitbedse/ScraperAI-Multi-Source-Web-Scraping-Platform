#!/usr/bin/env python3
"""
NIRF (India Rankings) scraper - ported from the standalone reference (D:\\NIRF\\main.py).

Data source: the official public HTML ranking pages at nirfindia.org. No JSON/CSV/API
endpoint was found: each category page is server-rendered HTML, and the per-institute
"More Details" block (parameter scores + PDF/graph links) is already embedded in that
HTML. So requests + BeautifulSoup is enough; no browser automation needed.

Layers (each testable / swappable on its own):
  Fetcher     session, UA, rate limit, retries with backoff, raw-HTML disk cache, offline mode
  Discovery   year -> category links -> extra pages (rank bands, "all" lists), by regex on <a href>
  Parsing     main table -> canonical records (nested parameter table per institute)
  Validation  duplicate keys, non-monotonic ranks, missing rank/score, parameter scores out of range
  Output      canonical JSON, flat CSV, manifest.json; incremental merge keyed by the dedup key

URL patterns (verified on the 2025 pages):
  {BASE}/Rankings/{year}/Ranking.html                 category index
  {BASE}/Rankings/{year}/{Slug}Ranking.html           top ranks (1-100 etc.)
  {BASE}/Rankings/{year}/{Slug}Ranking150.html        rank band 101-150 (also 200, 300, ...)
  {BASE}/Rankings/{year}/{Slug}RankingALL.html        participating institutions
  {BASE}/nirfpdfcdn/{year}/pdf/{Category}/{id}.pdf    institute data PDF
  {BASE}/nirfpdfcdn/{year}/graph/{Category}/{id}.jpg  score graph

CLI (from backend/):
  python -m app.scrapers.nirf.core --cache-dir html_cache -v                 # full run
  python -m app.scrapers.nirf.core --incremental --cache-dir html_cache      # daily cron
  python -m app.scrapers.nirf.core --categories Engineering Pharmacy Medical
  python -m app.scrapers.nirf.core --offline --cache-dir html_cache          # re-parse only
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = os.environ.get("NIRF_BASE_URL", "https://www.nirfindia.org").rstrip("/")
UA = "Mozilla/5.0 (compatible; scraperai-nirf/1.0; +contact-owner)"
SOURCE = "NIRF"
log = logging.getLogger("scraper.nirf")

CATEGORY_HREF = re.compile(r"/Rankings/(\d{4})/([A-Za-z]+)Ranking\.html$")
EXTRA_HREF = re.compile(r"/Rankings/(\d{4})/([A-Za-z]+)Ranking(\d+|ALL)\.html$", re.I)
PARAM_HDR = re.compile(r"^(.*?)\s*\((\d+(?:\.\d+)?)\)\s*$")

LABELS = {
    "Overall": "Overall", "University": "Universities", "College": "Colleges",
    "Research": "Research Institutions", "Engineering": "Engineering",
    "Management": "Management", "Pharmacy": "Pharmacy", "Medical": "Medical",
    "Dental": "Dental", "Law": "Law", "Architecture": "Architecture and Planning",
    "Agriculture": "Agriculture and Allied Sectors", "Innovation": "Innovation",
    "OPENUNIVERSITY": "Open University", "SKILLUNIVERSITY": "Skill University",
    "STATEPUBLICUNIVERSITY": "State Public University",
    "SDGInstitutions": "Sustainable Development Goals (SDGs)",
}


# --------------------------------------------------------------------------- fetching
class Fetcher:
    """Session reuse, identifiable UA, rate limit, retries (2s, 4s, 6s), timeout, raw-HTML cache, offline mode."""
    def __init__(self, delay=1.0, cache_dir: Path | None = None, offline=False, timeout=30,
                 base: str | None = None, sleep: Callable[[float], None] = time.sleep, retries=3, refresh=False):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.delay, self.cache_dir, self.offline, self.timeout = delay, cache_dir, offline, timeout
        self.base = (base or BASE).rstrip("/")
        self.sleep, self.retries = sleep, retries
        self.refresh = refresh                      # True: always download (and re-cache) instead of reading the cache
        self.last_error: Optional[str] = None       # why the last get() returned None (for error reporting)
        self.last_status: Optional[int] = None
        self.network_requests = 0
        self.cache_hits = 0
        self._last = 0.0
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, url):
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", url.replace(self.base, "").strip("/"))
        return self.cache_dir / (name or hashlib.md5(url.encode()).hexdigest()) if self.cache_dir else None

    def get(self, url, retries=None) -> str | None:
        self.last_error, self.last_status = None, None
        cp = self._cache_path(url)
        if cp and cp.exists() and (self.offline or not self.refresh):
            self.cache_hits += 1
            return cp.read_text(encoding="utf-8", errors="replace")
        if self.offline:
            self.last_error = "offline mode: page not in the HTML cache"
            return None
        for attempt in range(1, (retries or self.retries) + 1):
            wait = self.delay - (time.time() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = time.time()
            self.network_requests += 1
            try:
                r = self.s.get(url, timeout=self.timeout)
            except requests.Timeout as e:
                self.last_error = f"timeout: {e}"
                log.warning("GET %s timed out attempt %d", url, attempt)
                self.sleep(2 * attempt)
                continue
            except requests.RequestException as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("GET %s failed (%s) attempt %d", url, e, attempt)
                self.sleep(2 * attempt)
                continue
            self.last_status = r.status_code
            if r.status_code == 404:
                self.last_error = "HTTP 404"
                return None
            if r.status_code == 200:
                r.encoding = r.apparent_encoding or "utf-8"
                if cp:
                    cp.write_text(r.text, encoding="utf-8")
                return r.text
            self.last_error = f"HTTP {r.status_code}"
            log.warning("GET %s -> %s attempt %d", url, r.status_code, attempt)
            self.sleep(2 * attempt)
        return None


# --------------------------------------------------------------------------- discovery
def index_url(base: str, year: int) -> str:
    return f"{base}/Rankings/{year}/Ranking.html"


def discover_year(f: Fetcher, start: int | None = None) -> int | None:
    """Newest year whose Ranking.html exists and lists category links."""
    start = start or dt.date.today().year
    for y in range(start, 2015, -1):
        html = f.get(index_url(f.base, y))
        if html and discover_categories(html, y, f.base):
            return y
    return None


def discover_categories(html: str, year: int, base: str = BASE) -> dict[str, str]:
    """{slug: url} from the ranking index page navigation."""
    soup = BeautifulSoup(html, "lxml")
    out = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(f"{base}/Rankings/{year}/", a["href"])
        m = CATEGORY_HREF.search(href)
        if m and int(m.group(1)) == year:
            out.setdefault(m.group(2), href)
    return out


def select_categories(cats: dict[str, str], only) -> dict[str, str]:
    """Keep categories whose slug or label matches one of `only` (case-insensitive); empty = all."""
    if not only:
        return cats
    want = {o.strip().lower() for o in only}
    return {s: u for s, u in cats.items() if s.lower() in want or LABELS.get(s, s).lower() in want}


def discover_extra_pages(html: str, base_url: str, slug: str):
    """Rank-band and 'participating institutions' links on a category page."""
    soup = BeautifulSoup(html, "lxml")
    found = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        m = EXTRA_HREF.search(href)
        if m and m.group(2).lower() == slug.lower():
            found.setdefault(href, (m.group(3).upper(), " ".join(a.get_text(" ").split())))
    return found  # url -> (suffix, link text)


def band_label(suffix: str, link_text: str, category_slug: str) -> tuple[str, str | None]:
    m = re.search(r"(\d+)\s*[-–]\s*(\d+)", link_text or "")
    if suffix == "ALL":
        return "participating", None
    return "rank_band", (f"{m.group(1)}-{m.group(2)}" if m else f"up-to-{suffix}")


# --------------------------------------------------------------------------- parsing
def _num(text):
    t = (text or "").strip().replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


def _int(text):
    n = _num(text)
    return int(n) if n is not None and float(n).is_integer() else None


def _own_rows(table):
    return [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]


def _own_cells(tr):
    return [c for c in tr.find_all(["td", "th"], recursive=False)]


def _find_main_table(soup):
    for t in soup.find_all("table"):
        if t.find_parent("table"):
            continue
        hdr = " ".join(c.get_text(" ", strip=True).lower() for r in _own_rows(t)[:1] for c in _own_cells(r))
        if "institute id" in hdr or ("name" in hdr and "city" in hdr and "state" in hdr):
            return t
    return None


def _clean_name(cell) -> str:
    """Institution name = text of the cell before the 'More Details' toggle and nested table."""
    for s in cell.find_all(string=True):
        txt = " ".join(str(s).split())
        if txt and not txt.lower().startswith(("more details", "close")) and txt != "|":
            if s.find_parent("table") is None or s.find_parent("table") is cell:
                return txt
            break
    c = BeautifulSoup(str(cell), "lxml")
    for t in c.find_all(["table", "a"]):
        t.decompose()
    txt = " ".join(c.get_text(" ").split())
    return re.sub(r"\b(More Details|Close)\b|\|", "", txt).strip()


def _parse_param_table(cell):
    """Nested table: header row 'TLR (100)'..., value row -> {'TLR': {'score': 95.7, 'max': 100}}"""
    inner = cell.find("table")
    if not inner:
        return {}
    rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])] for tr in inner.find_all("tr")]
    rows = [r for r in rows if r and not all(re.fullmatch(r"-+", x) for x in r)]
    if len(rows) < 2:
        return {}
    out = {}
    for h, v in zip(rows[0], rows[1]):
        m = PARAM_HDR.match(h)
        key, mx = (m.group(1).strip(), float(m.group(2))) if m else (h.strip(), None)
        if key:
            out[key] = {"score": _num(v), "max": mx}
    return out


HEADER_PATTERNS = {"id": ("institute id", "id"), "name": ("name",), "city": ("city",),
                   "state": ("state",), "score": ("score",), "rank": ("rank",)}


def parse_ranking_page(html: str, *, url: str, year: int, category: str, slug: str,
                       list_type: str, rank_band: str | None):
    soup = BeautifulSoup(html, "lxml")
    table = _find_main_table(soup)
    if table is None:
        return [], ["no ranking table found"]
    rows = _own_rows(table)
    if not rows:
        return [], ["empty table"]
    headers = [c.get_text(" ", strip=True).lower() for c in _own_cells(rows[0])]
    idx = {}
    for i, h in enumerate(headers):
        for key, pats in HEADER_PATTERNS.items():
            if key not in idx and any(h == p or (p != "id" and p in h) for p in pats):
                idx[key] = i
    warnings, recs = [], []
    if "name" not in idx:
        return [], [f"unrecognised headers: {headers}"]
    for tr in rows[1:]:
        cells = _own_cells(tr)
        if len(cells) <= idx["name"]:
            continue
        g = lambda k: cells[idx[k]].get_text(" ", strip=True) if k in idx and idx[k] < len(cells) else None
        ncell = cells[idx["name"]]
        links = {"pdf": None, "graph": None}
        for a in ncell.find_all("a", href=True):
            h = urljoin(url, a["href"])
            if h.lower().endswith(".pdf"):
                links["pdf"] = h
            elif re.search(r"\.(jpg|jpeg|png)$", h, re.I):
                links["graph"] = h
        iid = g("id")
        rec = {
            "source": SOURCE,
            "ranking_year": year,
            "category": category,
            "category_slug": slug,
            "list_type": list_type,            # ranked | rank_band | participating
            "rank_band": rank_band,            # e.g. "101-150" for band pages
            "institution_id": iid or None,
            "institution_name": _clean_name(ncell),
            "city": g("city") or None,
            "state": g("state") or None,
            "rank": _int(g("rank")) if "rank" in idx else None,
            "score": _num(g("score")) if "score" in idx else None,
            "parameter_scores": _parse_param_table(ncell),
            "pdf_url": links["pdf"],
            "graph_url": links["graph"],
            "source_url": url,
        }
        if not rec["institution_name"]:
            warnings.append(f"row without name: {cells[0].get_text(strip=True)[:30]}")
            continue
        recs.append(rec)
    return recs, warnings


# --------------------------------------------------------------------------- incremental / dedup
def dedup_key(r: dict) -> tuple:
    """year + category + list_type + rank_band + institution_id (or name|city). Per category, never global."""
    iid = r.get("institution_id")
    if iid:
        return (r["ranking_year"], r["category"], r["list_type"], r["rank_band"], iid)
    return (r["ranking_year"], r["category"], r["list_type"], r["rank_band"],
            f"{r['institution_name'].lower()}|{(r['city'] or '').lower()}")


def output_json_path(out_dir: Path, year: int) -> Path:
    return out_dir / f"nirf_{year}.json"


def group_by_key(records: list) -> dict[tuple, list]:
    """{dedup_key: [records]}. A list, not one record: 'participating' pages have no institute id and
    list distinct institutions with the same name and city, which must not collapse into one."""
    out: dict[tuple, list] = {}
    for r in records:
        out.setdefault(dedup_key(r), []).append(r)
    return out


def load_existing_records(out_dir: Path, year: int) -> tuple[dict, list]:
    """Load existing JSON and return {dedup_key: [records]} + the list of records."""
    json_path = output_json_path(out_dir, year)
    if not json_path.exists():
        return {}, []
    with open(json_path, encoding="utf-8") as f:
        records = json.load(f)
    return group_by_key(records), records


def filter_new_records(new_records: list, existing_by_key: dict) -> tuple[list, list]:
    """Return (new_records_only, updated_records) by comparing with the existing groups."""
    new_only, updated = [], []
    for key, group in group_by_key(new_records).items():
        if key not in existing_by_key:
            new_only += group
        elif existing_by_key[key] != group:
            updated += group
    return new_only, updated


def merge_records(existing_by_key: dict, records: list) -> list:
    """Existing groups, with every key present in `records` replaced by this run's rows."""
    merged = dict(existing_by_key)
    merged.update(group_by_key(records))
    return [r for group in merged.values() for r in group]


# --------------------------------------------------------------------------- validation
def validate(records):
    """Audit-trail issues (strings); never raises."""
    issues = []
    seen = {}
    for r in records:
        key = dedup_key(r)
        if key in seen:
            issues.append(f"duplicate key {key}")
        seen[key] = 1
    by = {}
    for r in records:
        if r["list_type"] == "ranked":
            by.setdefault(r["category"], []).append(r)
    for cat, rs in by.items():
        ranks = [r["rank"] for r in rs if r["rank"] is not None]
        if ranks and ranks != sorted(ranks):
            issues.append(f"{cat}: ranks not monotonic")
        missing = [r["institution_name"] for r in rs if r["rank"] is None or r["score"] is None]
        if missing:
            issues.append(f"{cat}: {len(missing)} ranked rows missing rank/score")
        for r in rs:
            for k, v in r["parameter_scores"].items():
                if v["score"] is not None and v["max"] and not (0 <= v["score"] <= v["max"]):
                    issues.append(f"{cat}/{r['institution_id']}: {k} out of range")
    return issues


# --------------------------------------------------------------------------- runner
def plan_pages(fetcher: Fetcher, year: int, only, bands=True, all_list=True, manifest=None):
    """Discovery: index -> category pages -> extra pages. Yields (label, slug, url, html, list_type, band)."""
    manifest = manifest if manifest is not None else {"errors": []}
    index_html = fetcher.get(index_url(fetcher.base, year))
    if not index_html:
        raise SystemExit(f"Could not load ranking index for {year}")
    cats = select_categories(discover_categories(index_html, year, fetcher.base), only)
    for slug, url in cats.items():
        label = LABELS.get(slug, slug)
        html = fetcher.get(url)
        if not html:
            manifest["errors"].append(f"{label}: failed to load {url}")
            continue
        yield label, slug, url, html, "ranked", None
        if bands or all_list:
            for eurl, (suffix, text) in sorted(discover_extra_pages(html, url, slug).items()):
                lt, band = band_label(suffix, text, slug)
                if (lt == "rank_band" and not bands) or (lt == "participating" and not all_list):
                    continue
                ehtml = fetcher.get(eurl)
                if ehtml:
                    yield label, slug, eurl, ehtml, lt, band
                else:
                    manifest["errors"].append(f"{label}: failed to load {eurl}")


def scrape(year, only, fetcher: Fetcher, bands=True, all_list=True):
    records, manifest = [], {"year": year, "pages": [], "errors": []}
    for label, slug, purl, phtml, lt, band in plan_pages(fetcher, year, only, bands, all_list, manifest):
        recs, warns = parse_ranking_page(phtml, url=purl, year=year, category=label,
                                         slug=slug, list_type=lt, rank_band=band)
        records += recs
        manifest["pages"].append({"category": label, "url": purl, "list_type": lt,
                                  "rank_band": band, "rows": len(recs), "warnings": warns})
        log.info("%-34s %-12s %-8s %4d rows", label, lt, band or "", len(recs))
    manifest["validation_issues"] = validate(records)
    manifest["total_records"] = len(records)
    return records, manifest


def write_manifest(manifest, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def write_csv(records, path: Path):
    param_keys = sorted({k for r in records for k in r["parameter_scores"]})
    cols = ["ranking_year", "category", "list_type", "rank_band", "institution_id", "institution_name",
            "city", "state", "rank", "score"] + param_keys + ["pdf_url", "graph_url", "source_url"]
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in records:
            row = {c: r.get(c) for c in cols if c not in param_keys}
            for k in param_keys:
                row[k] = (r["parameter_scores"].get(k) or {}).get("score")
            w.writerow(row)


def write_outputs(records, manifest, out: Path, year: int):
    out.mkdir(parents=True, exist_ok=True)
    output_json_path(out, year).write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    write_manifest(manifest, out)
    write_csv(records, out / f"nirf_{year}.csv")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, help="ranking year (default: newest published)")
    ap.add_argument("--categories", nargs="*", help="slugs or names, e.g. Engineering 'Open University'")
    ap.add_argument("--out", type=Path, default=Path("nirf_output"))
    ap.add_argument("--cache-dir", type=Path, help="save/reuse raw HTML here")
    ap.add_argument("--offline", action="store_true", help="only read from --cache-dir")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--no-bands", action="store_true", help="skip rank-band pages (101-150 ...)")
    ap.add_argument("--no-all", action="store_true", help="skip 'participating institutions' pages")
    ap.add_argument("--incremental", action="store_true", help="merge new/changed records into existing output")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(message)s")

    f = Fetcher(a.delay, a.cache_dir, a.offline)
    year = a.year or discover_year(f)
    if not year:
        sys.exit("Could not find a published ranking year (is nirfindia.org reachable?)")
    log.info("Ranking year: %s", year)

    existing_by_key, existing_records = ({}, [])
    if a.incremental:
        existing_by_key, existing_records = load_existing_records(a.out, year)
        log.info("Loaded %d existing records from %s", len(existing_records), output_json_path(a.out, year))

    records, manifest = scrape(year, a.categories, f, not a.no_bands, not a.no_all)

    if a.incremental:
        new_only, updated = filter_new_records(records, existing_by_key)
        log.info("Incremental: %d new, %d updated, %d unchanged",
                 len(new_only), len(updated), len(records) - len(new_only) - len(updated))
        if not new_only and not updated:
            log.info("No new data found. Output unchanged.")
            return
        records = merge_records(existing_by_key, new_only + updated)
        manifest["incremental"] = {"new": len(new_only), "updated": len(updated), "total": len(records)}

    write_outputs(records, manifest, a.out, year)
    log.info("Wrote %d records to %s", len(records), a.out)
    if manifest["errors"] or manifest["validation_issues"]:
        log.warning("Check manifest.json: %d errors, %d validation issues",
                    len(manifest["errors"]), len(manifest["validation_issues"]))


if __name__ == "__main__":
    main()
