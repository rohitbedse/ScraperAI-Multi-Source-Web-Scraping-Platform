"""Extracts the parameters listed in parameters.yaml from Careers360 page state.

Which fields exist comes from the YAML. Each YAML key is bound to a small extractor below
(`COLLEGE`, `RANKING`, `COURSE`); a simple page value can instead be pulled with a `path:`
entry in the YAML with no code change. Keys with neither are returned as null and reported.
Nothing is invented: absent or empty values become None and are listed as missing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from bs4 import BeautifulSoup

from app.scrapers.careers360_nirf import config
from app.scrapers.careers360_nirf.schemas import Course, Parameter, ParametersFile, Seed


# =============================================================================== helpers
def get_path(obj: Any, path: str) -> Any:
    """Dotted lookup ('a.b.0.c'); None when any step is absent."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def html_to_text(html: Optional[str]) -> Optional[str]:
    if not html or not isinstance(html, str):
        return None
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
    return text or None


def is_missing(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def coerce(value: Any, ptype: str) -> Any:
    """Coerce to the YAML type; None when it cannot be done honestly."""
    if is_missing(value):
        return None
    try:
        if ptype == "str":
            return str(value).strip() or None
        if ptype == "int":
            return int(float(str(value).replace(",", "")))
        if ptype == "float":
            return float(str(value).replace(",", ""))
        if ptype == "bool":
            return value if isinstance(value, bool) else None
        if ptype == "list":
            return list(value) if isinstance(value, (list, tuple)) else [value]
    except (ValueError, TypeError):
        return None
    return value


# ======================================================================== normalization
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(year|yr|month|mon)s?", re.I)


def normalize_duration(raw: Any) -> tuple[Optional[str], Optional[int]]:
    """'48 Months' / '4 Years' -> ('4 years', 48). (None, None) if unparseable."""
    if is_missing(raw):
        return None, None
    m = _DURATION.search(str(raw))
    if not m:
        return None, None
    n = float(m.group(1))
    months = int(round(n * 12)) if m.group(2).lower() in ("year", "yr") else int(round(n))
    if months <= 0:
        return None, None
    if months % 12 == 0:
        y = months // 12
        return f"{y} year{'s' if y != 1 else ''}", months
    return f"{months} months", months


def normalize_fees(amount: Any, currency: Optional[str]) -> tuple[Optional[str], Optional[int], Optional[str]]:
    """(display, amount, currency). Zero/absent fees are treated as not provided."""
    try:
        amt = int(round(float(amount)))
    except (TypeError, ValueError):
        return None, None, None
    if amt <= 0:
        return None, None, None
    cur = (currency or "INR").upper()
    return f"{cur} {amt:,}", amt, cur


def normalize_exams(exams: Any) -> tuple[Optional[list[str]], list[str]]:
    """Short names (fallback: full name), de-duplicated in order. Returns (normalized, raw names)."""
    if not isinstance(exams, list):
        return None, []
    names, raw, seen = [], [], set()
    for e in exams:
        if not isinstance(e, dict):
            continue
        full = (e.get("name") or e.get("exam_name") or "").strip()
        short = (e.get("short_name") or e.get("exam_short_name") or full).strip()
        if full:
            raw.append(full)
        if short and short.lower() not in seen:
            seen.add(short.lower())
            names.append(short)
    return (names or None), raw


def classify_degree(label: Optional[str]) -> Optional[str]:
    """Careers360 degree label -> 'UG' | 'PG' | 'PhD', or None (diploma, certificate, combined, unknown)."""
    if not label:
        return None
    label = label.strip()
    if config.COMBINED_DEGREE.search(label):
        return None
    for level, rx in config.DEGREE_LEVEL_RULES:
        if rx.search(label):
            return level
    return None


# ======================================================================= college level
@dataclass
class CollegeContext:
    seed: Seed
    profile_url: str
    overview: dict                                  # window.INITIAL_STATE of the profile page
    param_by_key: dict[str, Parameter] = field(default_factory=dict)

    @property
    def institute(self) -> dict:
        return get_path(self.overview, "collegOverview.overview.institute_data") or {}

    @property
    def header(self) -> dict:
        return get_path(self.overview, "commonCollegeData.headerDetail.institution_data") or {}


def page_location(ctx_or_state: Any) -> tuple[Optional[str], Optional[str]]:
    """(city, state) as stated on the profile page."""
    state = ctx_or_state.overview if isinstance(ctx_or_state, CollegeContext) else ctx_or_state
    loc = get_path(state, "commonCollegeData.headerDetail.institution_data.current_location") or {}
    return (loc.get("city_name") or None), (loc.get("state_name") or None)


def _college_name(c: CollegeContext):
    return c.institute.get("name") or c.header.get("name")


def _university_name(c: CollegeContext):
    parent = c.header.get("parent_institution")
    if isinstance(parent, dict) and parent.get("name"):
        return parent["name"]
    if isinstance(parent, str) and parent.strip():
        return parent
    # Careers360 files universities/institutes under /university/; there the page entity is the university
    return _college_name(c) if "/university/" in c.profile_url else None


def _category(c: CollegeContext):
    value = (get_path(c.header, "ownership_value.value") or "").lower()
    if not value:
        return None
    if "private" in value:
        return "Private"
    if "public" in value or "government" in value or "govt" in value:
        return "Govt"
    return None   # e.g. mixed / deemed wording: not mapped, so not guessed


def _location(c: CollegeContext):
    city, state = page_location(c)
    return ", ".join(p for p in (city, state) if p) or None


COLLEGE: dict[str, tuple[Callable[[CollegeContext], Any], set[str]]] = {
    "standard_college_name": (_college_name, {"overview"}),
    "standard_university_name": (_university_name, {"overview"}),
    "college_category": (_category, {"overview"}),
    "description": (lambda c: html_to_text(c.institute.get("about_college")), {"overview"}),
    "source": (lambda c: config.SOURCE_NAME, set()),
    "url": (lambda c: c.profile_url, set()),
    "location": (_location, {"overview"}),
}


def extract_college(ctx: CollegeContext, params: list[Parameter]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in params:
        value = None
        if p.key in COLLEGE:
            value = COLLEGE[p.key][0](ctx)
        elif p.path:
            value = get_path(ctx.overview, p.path)
        value = coerce(value, p.type)
        if value is not None and p.allowed_values and value not in p.allowed_values:
            value = None
        out[p.key] = value
    return out


# ================================================================================ rankings
def _ranking_entries(state: dict) -> list[dict]:
    found: list[dict] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            if "ranking__ranking_authority" in o and o.get("overall_rank") not in (None, ""):
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(get_path(state, "collegOverview.overviewRanking") or {})
    return found


def _authority_rank(state: dict, authority: re.Pattern) -> Optional[str]:
    entries = [e for e in _ranking_entries(state) if authority.search(str(e["ranking__ranking_authority"]))]
    if not entries:
        return None
    best = max(entries, key=lambda e: e.get("ranking__year") or 0)
    return str(best["overall_rank"])


RANKING_AUTHORITIES = {
    "qs_world_ranking": re.compile(r"^QS\b", re.I),
    "times_higher_education_ranking": re.compile(r"times higher|^THE$", re.I),
}


def extract_rankings(ctx: CollegeContext, params: list[Parameter]) -> dict[str, Any]:
    """NIRF always comes from the seed. QS / THE only from the page, otherwise null."""
    out: dict[str, Any] = {}
    for p in params:
        if p.key == "nirf_ranking":
            value = ctx.seed.rank
        elif p.key in RANKING_AUTHORITIES:
            value = _authority_rank(ctx.overview, RANKING_AUTHORITIES[p.key])
        elif p.path:
            value = get_path(ctx.overview, p.path)
        else:
            value = None
        out[p.key] = coerce(value, p.type)
    return out


# ========================================================================= study destination
_DESTINATION_ALIASES = {
    "uk": ["united kingdom", "uk", "u.k."], "usa": ["united states", "usa", "u.s.a."],
}


def extract_destinations(ctx: CollegeContext, params: list[Parameter]) -> dict[str, Optional[bool]]:
    """True only when the page itself names the country (campus address). Never False/guessed."""
    address = str(ctx.institute.get("address_of_campus") or "")
    out: dict[str, Optional[bool]] = {}
    for p in params:
        names = _DESTINATION_ALIASES.get(p.label.strip().lower(), [p.label.strip().lower()])
        hit = any(re.search(rf"(?<![a-z]){re.escape(n)}(?![a-z])", address.lower()) for n in names)
        out[p.key] = True if hit else None
    return out


# ================================================================================ courses
@dataclass
class CourseContext:
    domain_map: dict[int, str] = field(default_factory=dict)
    admission_map: dict[int, str] = field(default_factory=dict)      # course id -> admission text


def domain_map_from(courses_state: dict) -> dict[int, str]:
    streams = get_path(courses_state, "courseFeesDetail.courseFilters.stream") or []
    return {s["id"]: s.get("label") or s.get("value") for s in streams if isinstance(s, dict) and "id" in s}


def admission_map_from(admission_state: dict) -> dict[int, str]:
    rows = get_path(admission_state, "admissionDetail.admissionCourses.courses_data_parent") or []
    out = {}
    for r in rows:
        text = html_to_text(r.get("admission_procedure"))
        if text and r.get("id") is not None:
            out[int(r["id"])] = text
    return out


@dataclass
class CourseBuild:
    row: dict
    detail: Optional[dict]          # courseDetailMain.course_data of the course page, if fetched
    ctx: CourseContext
    raw: dict = field(default_factory=dict)
    normalized: dict = field(default_factory=dict)


def _c_name(b: CourseBuild):
    return (b.row.get("course_name") or "").strip() or None


def _c_domain(b: CourseBuild):
    return b.ctx.domain_map.get(b.row.get("domain_id"))


def _c_eligibility(b: CourseBuild):
    text = html_to_text((b.detail or {}).get("eligibility_criteria"))
    if text:
        b.raw["eligibility"] = (b.detail or {}).get("eligibility_criteria")
    return text


def _c_details(b: CourseBuild):
    d = b.detail or {}
    html = get_path(d, "course_details.course_details") or d.get("course_overview")
    if html:
        b.raw["course_details"] = html
    return html_to_text(html)


def _c_duration(b: CourseBuild):
    raw = b.row.get("duration")
    if is_missing(raw) and b.detail:
        dd = b.detail.get("course_details") or {}
        if dd.get("duration"):
            raw = f"{dd['duration']} {dd.get('duration_type') or 'Months'}"
    display, months = normalize_duration(raw)
    if not is_missing(raw):
        b.raw["course_duration"] = raw
    if months:
        b.normalized["duration_months"] = months
    return display


def _c_fees(b: CourseBuild):
    amount = b.row.get("total_fees")
    cur = b.row.get("currency")
    display, amt, cur = normalize_fees(amount, cur)
    if not is_missing(amount):
        b.raw["fees"] = f"{amount} {b.row.get('currency') or ''}".strip()
    if amt:
        b.normalized["fees_amount"], b.normalized["fees_currency"] = amt, cur
    return display


def _c_exams(b: CourseBuild):
    names, raw = normalize_exams(b.row.get("exam_accepted"))
    if raw:
        b.raw["entrance_exams_accepted"] = raw
    return names


def _c_intake(b: CourseBuild):
    n = b.row.get("approved_intake")
    if not n:                       # 0 / None = not provided
        return None
    b.raw["course_intake"] = n
    return n


def _c_admission(b: CourseBuild):
    html = (b.detail or {}).get("admission_procedure")
    text = html_to_text(html)
    if text:
        b.raw["admission_process"] = html
        return text
    text = b.ctx.admission_map.get(b.row.get("id"))
    if text:
        b.raw["admission_process"] = text
    return text


COURSE: dict[str, tuple[Callable[[CourseBuild], Any], set[str]]] = {
    "course_name": (_c_name, {"courses"}),
    "domain": (_c_domain, {"courses"}),
    "eligibility": (_c_eligibility, {"course_detail"}),
    "course_details": (_c_details, {"course_detail"}),
    "course_duration": (_c_duration, {"courses"}),
    "fees": (_c_fees, {"courses"}),
    "entrance_exams_accepted": (_c_exams, {"courses"}),
    "research_areas": (lambda b: None, set()),          # Careers360 does not publish research areas
    "course_intake": (_c_intake, {"courses"}),
    "admission_process": (_c_admission, {"admission"}),
}


def course_dedupe_key(level: str, course: Course) -> tuple:
    name = re.sub(r"\s+", " ", str(course.fields.get("course_name") or "").lower()).strip()
    return (level, name, course.normalized.get("duration_months"), course.raw.get("study_mode"))


def build_course(row: dict, degree: Optional[str], ctx: CourseContext, fields: list[Parameter],
                 detail: Optional[dict] = None) -> Course:
    b = CourseBuild(row=row, detail=detail, ctx=ctx)
    values: dict[str, Any] = {}
    for p in fields:
        fn = COURSE.get(p.key, (None,))[0]
        values[p.key] = coerce(fn(b), p.type) if fn else None
    mode = get_path(row, "mode.study_mode_value")
    if mode:
        b.raw["study_mode"] = mode
    url = row.get("course_url")
    return Course(course_id=row.get("id"), url=f"{config.BASE_URL}/{url}" if url else None, degree=degree,
                  fields=values, raw=b.raw, normalized=b.normalized,
                  missing_fields=[k for k, v in values.items() if is_missing(v)])


def required_pages(params: ParametersFile) -> set[str]:
    """Logical page kinds needed for the parameters in the YAML (always includes the profile)."""
    need: set[str] = {"overview"}
    for p in params.college_parameters:
        need |= COLLEGE.get(p.key, (None, set()))[1]
        if p.path and p.section_hint in config.HINT_TO_SUBMENU:
            need.add(config.HINT_TO_SUBMENU[p.section_hint])
    for p in params.course_fields:
        need |= COURSE.get(p.key, (None, set()))[1]
    for p in params.ranking_category:
        if p.key != "nirf_ranking":
            need.add("overview")
    return need


def unbound_keys(params: ParametersFile) -> list[str]:
    """YAML keys with no extractor and no `path`: they will always be null."""
    out = []
    for group, table in ((params.college_parameters, COLLEGE), (params.course_fields, COURSE)):
        out += [p.key for p in group if p.key not in table and not p.path]
    out += [p.key for p in params.ranking_category
            if p.key != "nirf_ranking" and p.key not in RANKING_AUTHORITIES and not p.path]
    return out
