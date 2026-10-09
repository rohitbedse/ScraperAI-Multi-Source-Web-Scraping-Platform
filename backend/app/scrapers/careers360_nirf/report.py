"""Builds scrape_report.json from the saved records."""
from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from app.scrapers.careers360_nirf.extractor import is_missing, unbound_keys
from app.scrapers.careers360_nirf.schemas import CollegeRecord, ParametersFile, Seed

LIMITATIONS = [
    "research_areas: Careers360 does not publish them; always null.",
    "QS / Times Higher Education rankings: only present if the profile page lists them; otherwise null.",
    "study_destination: set True only when the page's campus address names the country; never False.",
    "Eligibility, course details and admission text come from one page per course; the number of course "
    "pages fetched per college is capped (see course_details_skipped).",
    "Courses whose degree is diploma, certificate or a combined/dual programme are not UG/PG/PhD and are skipped "
    "(courses_skipped_other_level).",
]


def _pct(filled: int, total: int) -> float:
    return round(100.0 * filled / total, 1) if total else 0.0


def build_report(seeds: list[Seed], records: dict[str, CollegeRecord], params: ParametersFile,
                 run: dict[str, Any], failed_checkpoint_ids: Optional[list[str]] = None) -> dict[str, Any]:
    recs = [records[s.id] for s in seeds if s.id in records]
    match_counts = Counter(r.match.status for r in recs)
    status_counts = Counter(r.status for r in recs)
    scraped = [r for r in recs if r.status in ("scraped", "partial")]

    courses_per_level = {lvl: sum(len(r.courses.get(lvl, [])) for r in recs) for lvl in params.degree_levels}

    college_scope: dict[str, list[bool]] = {}
    for r in scraped:
        for group in (r.college, r.rankings, r.study_destination):
            for k, v in group.items():
                college_scope.setdefault(k, []).append(not is_missing(v))
    course_scope: dict[str, list[bool]] = {p.key: [] for p in params.course_fields}
    for r in recs:
        for lst in r.courses.values():
            for c in lst:
                for p in params.course_fields:
                    course_scope[p.key].append(not is_missing(c.fields.get(p.key)))

    def cov(d: dict[str, list[bool]]) -> dict[str, dict]:
        return {k: {"filled": sum(v), "total": len(v), "percent": _pct(sum(v), len(v))} for k, v in d.items()}

    completeness = []
    for r in recs:
        if r.status not in ("scraped", "partial", "failed"):
            completeness.append({"id": r.seed.id, "name": r.seed.name, "status": r.status, "completeness": 0.0})
            continue
        slots = [not is_missing(v) for g in (r.college, r.rankings, r.study_destination) for v in g.values()]
        slots += [not is_missing(c.fields.get(p.key)) for lst in r.courses.values() for c in lst
                  for p in params.course_fields]
        completeness.append({"id": r.seed.id, "name": r.seed.name, "status": r.status,
                             "completeness": _pct(sum(slots), len(slots))})

    return {
        "seed_count": len(seeds),
        "records_in_output": len(recs),
        "match": {"matched": match_counts["matched"], "ambiguous": match_counts["ambiguous"],
                  "unmatched": match_counts["unmatched"]},
        "scrape": {"scraped": status_counts["scraped"], "partial": status_counts["partial"],
                   "failed": status_counts["failed"]},
        "courses_per_level": courses_per_level,
        "courses_total": sum(courses_per_level.values()),
        "duplicates_skipped": sum(r.duplicates_skipped for r in recs),
        "courses_skipped_other_level": sum(r.courses_skipped_other_level for r in recs),
        "course_details_skipped": sum(r.course_details_skipped for r in recs),
        "parameter_coverage": {"college_level": cov(college_scope), "course_level": cov(course_scope)},
        "unbound_parameters": unbound_keys(params),
        "per_college_completeness": completeness,
        "failed_ids": sorted(r.seed.id for r in recs if r.status == "failed"),
        "ambiguous_ids": sorted(r.seed.id for r in recs if r.status == "ambiguous"),
        "unmatched_ids": sorted(r.seed.id for r in recs if r.status == "unmatched"),
        "run": run,
        "limitations": LIMITATIONS,
    }
