"""Paths, rate limits and timeouts for the Careers360 NIRF scraper."""
import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
FIXTURES_DIR = BASE_DIR / "fixtures"

SEED_FILE = DATA_DIR / "nirf_seed.json"
PARAMETERS_FILE = DATA_DIR / "parameters.yaml"

# everything this scraper writes stays inside its own output folder
COLLEGES_FILE = OUTPUT_DIR / "colleges_data.json"
REPORT_FILE = OUTPUT_DIR / "scrape_report.json"
CHECKPOINT_FILE = OUTPUT_DIR / "checkpoint.json"
LOG_FILE = OUTPUT_DIR / "scraper.log"
CACHE_DIR = OUTPUT_DIR / "cache"
SITEMAP_CACHE_FILE = CACHE_DIR / "sitemap_college_view.xml"

SOURCE_NAME = "Careers360"
BASE_URL = "https://www.careers360.com"
ALLOWED_HOSTS = {"www.careers360.com"}
ROBOTS_URL = f"{BASE_URL}/robots.txt"
SITEMAP_URL = f"{BASE_URL}/sitemap-college-view.xml"
SITEMAP_TTL_DAYS = 7

# ---- politeness (all overridable through the environment)
CONTACT = os.environ.get("CAREERS360_CONTACT", "unset")
USER_AGENT = f"NIRFResearchScraper/1.0 (identifiable bot; contact: {CONTACT})"
REQUEST_DELAY_SECONDS = float(os.environ.get("CAREERS360_DELAY", "2.0"))
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("CAREERS360_TIMEOUT", "30"))
SITEMAP_TIMEOUT_SECONDS = 120.0
MAX_RETRIES = int(os.environ.get("CAREERS360_RETRIES", "3"))
BACKOFF_BASE_SECONDS = 2.0
BLOCK_STATUSES = {403, 429}
CAPTCHA_MARKERS = ("captcha", "are you a robot", "verify you are human", "access denied")
MAX_CONSECUTIVE_BLOCKED_COLLEGES = 3        # abort the run: the site keeps refusing us

# ---- matching
MATCH_THRESHOLD = 0.90          # minimum name similarity to accept a candidate
MATCH_MARGIN = 0.04             # a second candidate this close to the best makes it ambiguous
CANDIDATES_RECORDED = 5
AMBIGUOUS_PROBE_LIMIT = 3       # near-tied candidates whose profile page is opened to check city + state

# ---- scope limits
MAX_LISTING_PAGES_PER_DEGREE = 60
# Eligibility / details / admission text live on one page per course. 0 = unlimited.
MAX_COURSE_DETAIL_PAGES = int(os.environ.get("CAREERS360_MAX_COURSE_DETAILS", "30"))

# ---- Careers360 degree label -> degree level (first matching rule wins; None = not UG/PG/PhD)
DEGREE_LEVEL_RULES: list[tuple[str, "re.Pattern[str]"]] = [
    ("PhD", re.compile(r"^(ph\.?\s?d|doctor)", re.I)),
    ("UG", re.compile(r"^(mbbs|bds|bams|bhms|b\.?\s?[a-z]|bachelor|llb|bs\b)", re.I)),
    ("PG", re.compile(r"^(m\.?\s?[a-z]|master|mba|pgdm|llm|post\s?grad|pg\b|md\b|ms\b)", re.I)),
]
# labels that join two levels in one programme (e.g. "B.Tech M.Tech", "BS and MS") are not classified
COMBINED_DEGREE = re.compile(r"\b(b\.?\s?tech|bs|b\.?sc|ba|bba|b\.?com)\b.*\b(m\.?\s?tech|ms|m\.?sc|ma|mba)\b|dual|integrated",
                             re.I)

# parameters.yaml `section_hint` -> Careers360 sub-menu name (for generic `path` parameters)
HINT_TO_SUBMENU = {"overview": "overview", "courses": "courses", "fees": "courses",
                   "admission": "admission", "rankings": "overview"}
