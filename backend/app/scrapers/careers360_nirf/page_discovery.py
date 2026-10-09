"""Finds the sub-pages Careers360 lists for a college, and reads page state / listing structure."""
from __future__ import annotations

import json
import re
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from app.scrapers.careers360_nirf import config
from app.scrapers.careers360_nirf.extractor import get_path


class LayoutChanged(Exception):
    """The page no longer carries the embedded state we parse."""


_MARKER = "window.INITIAL_STATE="


_JS_FUNCTION = re.compile(r"(?<=[:\[,])function\b[^{]*\{")


def _blank_js_functions(seg: str) -> str:
    """Replace raw JS function literals (a serialized failed request on some pages carries
    `"adapter":function httpAdapter(config) {...}`) with null. Braces inside string literals are skipped."""
    out, pos = [], 0
    while True:
        m = _JS_FUNCTION.search(seg, pos)
        if not m:
            out.append(seg[pos:])
            return "".join(out)
        depth, i, quote = 1, m.end(), None
        while i < len(seg) and depth:
            ch = seg[i]
            if quote:
                if ch == "\\":
                    i += 1
                elif ch == quote:
                    quote = None
            elif ch in "\"'`":
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        if depth:                       # unbalanced: leave it alone, the strict parse reports the error
            out.append(seg[pos:])
            return "".join(out)
        out.append(seg[pos:m.start()] + "null")
        pos = i


def parse_state(html: str) -> dict:
    """Return the page's embedded `window.INITIAL_STATE` JSON (bare `undefined` values become null)."""
    i = html.find(_MARKER)
    if i < 0:
        raise LayoutChanged("window.INITIAL_STATE not found")
    start = i + len(_MARKER)
    end = html.find("</script>", start)
    seg = html[start: end if end > 0 else None].strip().rstrip(";")
    seg = re.sub(r"(?<=[:\[,])undefined(?=[,}\]])", "null", seg)
    try:
        # the live pages put further statements after the state in the same <script>
        # (`window.INITIAL_STATE={...};window.nonCriticalCssString=...`): take the first JSON value only
        state, _ = json.JSONDecoder().raw_decode(seg)
    except json.JSONDecodeError as exc:
        try:                            # strict parse failed: retry once with JS function literals blanked
            state, _ = json.JSONDecoder().raw_decode(_blank_js_functions(seg))
        except json.JSONDecodeError:
            raise LayoutChanged(f"INITIAL_STATE is not valid JSON: {exc}") from exc
    if not isinstance(state, dict):
        raise LayoutChanged("INITIAL_STATE is not an object")
    return state


def discover_pages(state: dict, profile_url: str) -> dict[str, str]:
    """{submenu_name: absolute URL} from the college's own navigation (overview, courses, fees, admission, ...)."""
    menu = get_path(state, "commonCollegeData.subMenuData") or []
    pages: dict[str, str] = {}
    for m in menu:
        name, url = m.get("submenu_name"), m.get("url")
        if name and url:
            pages[name] = url if url.startswith("http") else f"{config.BASE_URL}/{url.lstrip('/')}"
    pages.setdefault("overview", profile_url)
    return pages


def clean_url(url: str) -> str:
    """Drop the query string (the site's own filter links carry tracking-style params)."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def with_page(url: str, page: int) -> str:
    return url if page <= 1 else f"{clean_url(url)}?page={page}"


def degree_filters(courses_state: dict) -> list[dict]:
    """[{label, id, url}] of the degree pages listed on the courses page."""
    rows = get_path(courses_state, "courseFeesDetail.coursesDetail.inline_filters.degree") or []
    return [{"label": r.get("value"), "id": r.get("id"), "url": clean_url(r["url"])}
            for r in rows if isinstance(r, dict) and r.get("url") and r.get("value")]


def listing(courses_state: dict) -> tuple[list[dict], int]:
    """(result rows, total pages) of a course listing page."""
    detail = get_path(courses_state, "courseFeesDetail.coursesDetail") or {}
    rows = detail.get("results") or []
    total = int(get_path(detail, "pagination.total_page") or 1)
    return rows, total


def course_detail(state: dict) -> Optional[dict]:
    return get_path(state, "courseFeesDetail.courseDetailMain.course_data")
