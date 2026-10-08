"""
Swayam course scraper (Upcoming + Ongoing)  ->  Upskilling_Courses_<date>.json
Playwright for the browser, Pydantic for the output schema.

Flow
  Stage A  open swayam.gov.in/explorer
           for each tab (Upcoming, Ongoing):
             click tab -> click LOAD MORE until no new course URL appears
             -> keep every card once (URL is the key, so the repeat-cards bug does not matter)
  Stage B  open every unique course URL once (N pages in parallel)
           read left panel + Course Information, Summary, Instructor Bio, Course Certificate
  Output   validate every row with Pydantic (bad rows go to a Validation_Errors sheet)
           -> one JSON file

Setup (Windows, normal internet):
    pip install playwright pydantic
    python -m playwright install chromium

Run:
    python swayam_scraper.py --limit 10 --show     # quick test, watch the browser
    python swayam_scraper.py                       # full run
    python swayam_scraper.py --reuse-cards         # skip Stage A, reuse swayam_cards.json

Progress is saved to swayam_progress.jsonl, so a stopped run continues where it left off.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
import traceback
from datetime import date, datetime
from typing import Literal, Optional

from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PAGE_TIMEOUT_MS = int(os.environ.get("SWAYAM_PAGE_TIMEOUT_MS", "90000"))
EXPLORER = os.environ.get("SWAYAM_EXPLORER", "https://swayam.gov.in/explorer")
TABS = [("Upcoming", "Upcoming (Enrollment Open)"), ("Ongoing", "Ongoing (Enrollment Closed)")]
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("SCRAPER_STATE_DIR", HERE)
PROGRESS = os.path.join(STATE_DIR, "swayam_progress.jsonl")
CARDS_FILE = os.path.join(STATE_DIR, "swayam_cards.json")

SUMMARY_LABELS = [
    "Course Status", "Course Type", "Course Language", "Course Level", "Category", "Duration",
    "Credit Points", "Start Date", "End Date", "Enrollment Ends", "Exam Date", "Exam Registration Ends",
]
EMPTY = {"", "-", "—", "–", "â€”", "n/a", "na", "none", "nil", "tba"}


# =============================================================================== schema
class ExplorerCard(BaseModel):
    url: str
    card_name: str = ""
    source: str = ""
    card_duration: str = ""
    explorer_tab: str


class Course(BaseModel):
    """One course in the output JSON = the mind-map parameters."""
    model_config = ConfigDict(str_strip_whitespace=True)

    course_name: str = Field(min_length=1)   # a course is literally called "R"
    url: str = Field(pattern=r"^https?://")
    source: Optional[str] = None
    institute: Optional[str] = None
    description: Optional[str] = None
    subject_area: Optional[str] = None
    course_type: Optional[Literal["Core", "Elective", "Not Applicable"]] = None
    course_format: Optional[Literal["Video / Self-paced", "Video / Scheduled"]] = None
    difficulty_level: Optional[Literal["Beginner", "Intermediate", "Advanced"]] = None
    academic_level: Optional[str] = None          # "UG", "PG", "UG, PG", "PhD", ...
    course_duration: Optional[str] = None
    skills_learned: Optional[str] = None
    intended_audience: Optional[str] = None
    prerequisites: Optional[str] = None
    language: Optional[str] = None
    instructor: Optional[str] = None
    instructor_bio: Optional[str] = None
    certificate: Optional[str] = None
    credit_points: Optional[float] = Field(default=None, ge=0, le=40)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    enrollment_date: Optional[date] = None
    exam_date: Optional[date] = None
    exam_reg_date: Optional[date] = None
    deadline: Optional[date] = None
    important_dates: Optional[str] = None
    live_status: Optional[Literal["Upcoming", "Live", "Past"]] = None
    course_status_raw: Optional[str] = None
    explorer_tab: Optional[str] = None
    reviews: Optional[str] = None                  # not shown on Swayam course pages
    no_of_enrolled_users: Optional[int] = Field(default=None, ge=0)

    @field_validator("*", mode="before")
    @classmethod
    def blank_to_none(cls, v):
        if isinstance(v, str) and v.strip().lower() in EMPTY:
            return None
        return v

    @field_validator("start_date", "end_date", "enrollment_date", "exam_date", "exam_reg_date",
                     "deadline", mode="before")
    @classmethod
    def parse_date(cls, v):
        if v is None or isinstance(v, date):
            return v
        s = re.sub(r"\s+", " ", str(v)).strip()
        s = re.sub(r"\s*(IST|GMT|UTC)$", "", s, flags=re.I)   # "08 Dec 2026 IST"
        s = re.sub(r"\s*\(.*\)$", "", s)                    # "08 Dec 2026 (Sunday)"
        if not re.search(r"\d", s):          # "—", "TBA", "To be announced" ... -> empty
            return None
        for fmt in ("%d %b %Y", "%d %B %Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%b %d, %Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                pass
        raise ValueError(f"unknown date format: {s!r}")

    @field_validator("no_of_enrolled_users", mode="before")
    @classmethod
    def parse_int(cls, v):
        if isinstance(v, str):
            digits = re.sub(r"[^\d]", "", v)
            return int(digits) if digits else None
        return v

    @field_validator("credit_points", mode="before")
    @classmethod
    def parse_float(cls, v):
        if isinstance(v, str):
            m = re.search(r"\d+(\.\d+)?", v)
            return float(m.group()) if m else None
        return v


# =============================================================================== stage A
CARDS_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  let cards = document.querySelectorAll('div.course-list > div.col-md-4');
  if (!cards.length) cards = document.querySelectorAll('course-card');
  cards.forEach(c => {
    const a = c.querySelector('a[href*="/preview"]') || c.querySelector('a[href]');
    if (!a || !a.href || seen.has(a.href)) return;
    seen.add(a.href);
    const titleEl = c.querySelector('h1,h2,h3,h4,.title,[class*="title"]') || a;
    const lines = (c.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    const di = lines.findIndex(l => /week|self paced|starts|month|hour/i.test(l));
    const dur = di >= 0 ? lines[di] : '';
    // card text order: title, instructor, institute, SOURCE (IGNOU/AICTE/NPTEL...), duration
    let source = di > 0 ? lines[di - 1] : '';
    if (source.length > 40) source = '';
    if (!source) { const img = c.querySelector('img[alt]'); source = img ? img.alt.trim() : ''; }
    out.push({url: a.href.split('?')[0], card_name: (titleEl.innerText || lines[0] || '').trim(),
              source: source, card_duration: dur});
  });
  return out;
}
"""


async def collect_cards(page, max_clicks: int, on_progress=None, check=None) -> list[ExplorerCard]:
    cards: dict[str, ExplorerCard] = {}
    for tab_key, tab_label in TABS:
        await page.goto(EXPLORER, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        await page.locator("div.course-list > div.col-md-4, course-card").first.wait_for(timeout=PAGE_TIMEOUT_MS)
        tab = page.get_by_text(tab_label, exact=False).first
        await tab.click()
        await page.wait_for_timeout(4000)

        tab_urls: set[str] = set()
        idle = 0
        for click in range(max_clicks + 1):
            for raw in await page.evaluate(CARDS_JS):
                tab_urls.add(raw["url"])
                if raw["url"] in cards:
                    c = cards[raw["url"]]
                    if tab_key not in c.explorer_tab:
                        c.explorer_tab += f", {tab_key}"
                    # prefer a dated card over a "Self Paced" copy of the same course
                    if "self paced" in c.card_duration.lower() and raw["card_duration"]:
                        c.card_duration = raw["card_duration"]
                else:
                    cards[raw["url"]] = ExplorerCard(explorer_tab=tab_key, **raw)
            new_count = len(tab_urls)
            print(f"[{tab_key}] LOAD MORE x{click:<3} unique courses in tab: {new_count}")
            if check:
                check()
            if on_progress:
                on_progress(tab_key, new_count, len(cards))
            idle = idle + 1 if click and new_count == prev else 0
            prev = new_count
            if idle >= 3:
                print(f"[{tab_key}] 3 clicks without new courses -> done")
                break
            btn = page.locator("#load-more-button, paper-button:has-text('LOAD MORE')").first
            visible = False
            for _ in range(10):                       # button can vanish for a moment while loading
                if await btn.count() and await btn.is_visible():
                    visible = True
                    break
                await page.wait_for_timeout(1000)
            if not visible:
                print(f"[{tab_key}] LOAD MORE gone -> done")
                break
            await btn.scroll_into_view_if_needed()
            await btn.click()
            # wait until new cards really appear (slow site), max 20 s
            for _ in range(20):
                await page.wait_for_timeout(1000)
                now = {r["url"] for r in await page.evaluate(CARDS_JS)}
                if len(now | tab_urls) > new_count:
                    break

    out = list(cards.values())
    with open(CARDS_FILE, "w", encoding="utf-8") as f:
        json.dump([c.model_dump() for c in out], f, ensure_ascii=False, indent=1)
    return out


# =============================================================================== stage B
SUMMARY_JS = r"""
(labels) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = {};
  const divs = Array.from(document.querySelectorAll('div,dt,th,td,span'));
  for (const lab of labels) {
    const el = divs.find(d => norm(d.innerText) === lab && d.nextElementSibling);
    if (el) out[lab] = norm(el.nextElementSibling.innerText);
  }
  return out;
}
"""

PANEL_JS = r"""
() => {
  const main = document.querySelector('main.flex-col') || document.querySelector('main') || document.body;
  return main ? main.innerText : '';
}
"""


def clean_panel(text: str, heading: str) -> str:
    out = []
    for line in (l.strip() for l in text.splitlines()):
        if not line or line.lower() in {heading.lower(), "join the course", "sign in"}:
            continue
        if line.lower().startswith("share"):
            break
        out.append(line)
    return "\n".join(out).strip()


async def open_tab(page, name: str) -> Optional[str]:
    btn = page.locator("button", has_text=name).first
    if not await btn.count():
        return None
    await btn.click()
    await page.wait_for_timeout(800)
    return clean_panel(await page.evaluate(PANEL_JS), name)


async def scrape_course(ctx, card: ExplorerCard, sem: asyncio.Semaphore) -> dict:
    async with sem:
        page = await ctx.new_page()
        rec = {"url": card.url, "_error": ""}
        try:
            await page.goto(card.url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            await page.locator("h1").first.wait_for(timeout=int(PAGE_TIMEOUT_MS * 0.66))
            await page.wait_for_timeout(800)

            aside = page.locator("aside").first
            aside_text = await aside.inner_text() if await aside.count() else ""
            rec["course_name"] = (await page.locator("h1").first.inner_text()).strip()
            m = re.search(r"^\s*By\s+(.+)$", aside_text, re.M)
            rec["instructor"] = m.group(1).strip() if m else None
            m = re.search(r"Learners enrolled:\s*([\d,]+)", aside_text)
            rec["enrolled"] = m.group(1) if m else None
            lines = [l.strip(" |") for l in aside_text.splitlines() if l.strip(" |")]
            for i, l in enumerate(lines):
                if l.startswith("Learners enrolled") and i:
                    rec["institute"] = lines[i - 1] if not lines[i - 1].startswith("By ") else None

            rec["info"] = await open_tab(page, "Course Information") or clean_panel(
                await page.evaluate(PANEL_JS), "Course Information")
            if await open_tab(page, "Summary") is not None:
                rec["summary"] = await page.evaluate(SUMMARY_JS, SUMMARY_LABELS)
            rec["outline"] = await open_tab(page, "Course outline") or await open_tab(page, "Course Layout")
            rec["bio"] = await open_tab(page, "Instructor Bio")
            rec["certificate"] = await open_tab(page, "Course Certificate")
        except PWTimeout as e:
            rec["_error"] = "timeout"
            rec["_exc"], rec["_tb"] = type(e).__name__, traceback.format_exc()
        except Exception as e:  # noqa: BLE001  one bad course must not stop the run
            rec["_error"] = str(e)[:200]
            rec["_exc"], rec["_tb"] = type(e).__name__, traceback.format_exc()
        finally:
            await page.close()
        return rec


# =============================================================================== mapping
STOP = r"intended audience|pre-?requisites?|industry support|industries? that will recognize|course layout|" \
       r"course outline|books|references|learning outcomes?|course objectives?|summary"


def section(text: Optional[str], keys: str) -> Optional[str]:
    if not text:
        return None
    # heading must start a line and end with ":" / "-", so words in the middle of a sentence don't match
    m = re.search(rf"(?:^|\n)\s*(?:{keys})\s*[:\-]\s*(.+?)(?=\n\s*(?:{STOP})\s*[:\-]|\Z)", text, re.I | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:1500] if m else None


def academic_level(v: Optional[str]) -> Optional[str]:
    v = (v or "").lower()
    out = [tag for tag, pat in (("UG", r"under|\bug\b"), ("PG", r"post|\bpg\b"), ("PhD", r"phd|doctor"))
           if re.search(pat, v)]
    return ", ".join(out) or (v.title() or None)


def live_status(v: Optional[str]) -> Optional[str]:
    v = (v or "").lower()
    if "upcoming" in v:
        return "Upcoming"
    if any(k in v for k in ("ongoing", "progress", "live")):
        return "Live"
    if any(k in v for k in ("complete", "archiv", "past", "closed")):
        return "Past"
    return None


def course_type(v: Optional[str]) -> Optional[str]:
    v = (v or "").lower()
    return "Core" if "core" in v else "Elective" if "elective" in v else "Not Applicable" if v else None


def difficulty(*texts: Optional[str]) -> Optional[str]:
    t = " ".join(x or "" for x in texts).lower()
    if re.search(r"\badvanced\b", t):
        return "Advanced"
    if re.search(r"\bintermediate\b", t):
        return "Intermediate"
    if re.search(r"\b(beginner|basics?|introduct\w*|fundamentals?|elementary|foundation)\b", t):
        return "Beginner"
    return None


def to_row(card: ExplorerCard, rec: dict) -> dict:
    s = rec.get("summary") or {}
    info = rec.get("info") or ""
    dates = {k: s.get(lab) for k, lab in [("Start", "Start Date"), ("End", "End Date"),
                                          ("Enrollment ends", "Enrollment Ends"), ("Exam", "Exam Date"),
                                          ("Exam registration ends", "Exam Registration Ends")]}
    self_paced = "self paced" in (card.card_duration + (s.get("Duration") or "")).lower()
    return {
        "course_name": rec.get("course_name") or card.card_name,
        "url": card.url,
        "source": card.source,
        "institute": rec.get("institute"),
        "description": info[:3000] or None,
        "subject_area": s.get("Category"),
        "course_type": course_type(s.get("Course Type")),
        "course_format": "Video / Self-paced" if self_paced else "Video / Scheduled",
        "difficulty_level": difficulty(rec.get("course_name"), info[:600]),
        "academic_level": academic_level(s.get("Course Level")),
        "course_duration": s.get("Duration") or card.card_duration,
        "skills_learned": section(info, r"learning outcomes?|course objectives?|what you will learn")
                          or (rec.get("outline") or "")[:1500] or None,
        "intended_audience": section(info, r"intended audience"),
        "prerequisites": section(info, r"pre-?requisites?"),
        "language": s.get("Course Language"),
        "instructor": rec.get("instructor"),
        "instructor_bio": (rec.get("bio") or "")[:2000] or None,
        "certificate": (rec.get("certificate") or "")[:1500] or None,
        "credit_points": s.get("Credit Points"),
        "start_date": dates["Start"],
        "end_date": dates["End"],
        "enrollment_date": dates["Enrollment ends"],
        "exam_date": dates["Exam"],
        "exam_reg_date": dates["Exam registration ends"],
        "deadline": dates["Enrollment ends"] if (dates["Enrollment ends"] or "").strip() not in EMPTY
                    else dates["Exam registration ends"],
        "important_dates": "; ".join(f"{k}: {v}" for k, v in dates.items()
                                     if v and v.strip().lower() not in EMPTY) or None,
        "live_status": live_status(s.get("Course Status")),
        "course_status_raw": s.get("Course Status"),
        "explorer_tab": card.explorer_tab,
        "no_of_enrolled_users": rec.get("enrolled"),
    }


def validate(rows: list[dict]) -> tuple[list[Course], list[dict]]:
    good, bad = [], []
    for row in rows:
        try:
            good.append(Course.model_validate(row))
        except ValidationError as e:
            # keep the course: drop only the bad fields, log what was wrong
            bad_fields = {err["loc"][0] for err in e.errors() if err["loc"]}
            for err in e.errors():
                bad.append({"url": row.get("url"), "field": ".".join(map(str, err["loc"])),
                            "value": str(row.get(err["loc"][0]) if err["loc"] else "")[:200],
                            "error": err["msg"]})
            fixed = {k: v for k, v in row.items() if k not in bad_fields}
            try:
                good.append(Course.model_validate(fixed))
            except ValidationError:
                pass  # name/url missing -> row is unusable, already logged
    return good, bad


# =============================================================================== main
async def main(args) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.show)
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900})

        if args.reuse_cards and os.path.exists(CARDS_FILE):
            cards = [ExplorerCard(**c) for c in json.load(open(CARDS_FILE, encoding="utf-8"))]
            print(f"Stage A skipped: {len(cards)} cards from {CARDS_FILE}")
        else:
            page = await ctx.new_page()
            cards = await collect_cards(page, args.max_clicks)
            await page.close()
            print(f"Stage A done: {len(cards)} unique courses")
        if args.limit:
            cards = cards[: args.limit]

        done: dict[str, dict] = {}
        if os.path.exists(PROGRESS) and not args.fresh:
            for line in open(PROGRESS, encoding="utf-8"):
                r = json.loads(line)
                if not r.get("_error"):
                    done[r["url"]] = r
        todo = [c for c in cards if c.url not in done]
        print(f"Stage B: {len(done)} cached, {len(todo)} to open, {args.workers} in parallel")

        sem = asyncio.Semaphore(args.workers)
        t0 = time.time()
        with open(PROGRESS, "a", encoding="utf-8") as prog:
            tasks = [asyncio.create_task(scrape_course(ctx, c, sem)) for c in todo]
            for i, fut in enumerate(asyncio.as_completed(tasks), 1):
                rec = await fut
                done[rec["url"]] = rec
                prog.write(json.dumps(rec, ensure_ascii=False) + "\n")
                prog.flush()
                if i % 10 == 0 or i == len(tasks):
                    print(f"  {i}/{len(tasks)} courses  ({time.time() - t0:.0f}s)")
        await browser.close()

    rows = [to_row(c, done.get(c.url, {})) for c in cards]
    courses, errors = validate(rows)
    scrape_errors = [{"url": u, "error": r["_error"]} for u, r in done.items() if r.get("_error")]

    status_count: dict[str, int] = {}
    for c in courses:
        key = c.live_status or "Unknown"
        status_count[key] = status_count.get(key, 0) + 1

    out = os.path.join(HERE, f"Upskilling_Courses_{datetime.now():%Y%m%d}.json")
    result = {
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "source": EXPLORER,
        "total_courses": len(courses),
        "live_status_count": status_count,
        "courses": [c.model_dump(mode="json") for c in courses],   # dates -> "YYYY-MM-DD", missing -> null
        "validation_errors": errors,
        "scrape_errors": scrape_errors,
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\nSaved {len(courses)} courses -> {out}")
    for k, v in status_count.items():
        print(f"  {k:<10} {v}")
    print(f"Validation errors: {len(errors)}   Scrape errors: {len(scrape_errors)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only scrape the first N courses (test)")
    ap.add_argument("--workers", type=int, default=5, help="course pages open at the same time")
    ap.add_argument("--max-clicks", type=int, default=120, help="safety cap on LOAD MORE clicks per tab")
    ap.add_argument("--show", action="store_true", help="show the browser window")
    ap.add_argument("--reuse-cards", action="store_true", help="skip Stage A, reuse swayam_cards.json")
    ap.add_argument("--fresh", action="store_true", help="ignore swayam_progress.jsonl")
    asyncio.run(main(ap.parse_args()))