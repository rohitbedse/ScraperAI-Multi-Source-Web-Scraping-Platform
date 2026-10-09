"""Shared test helpers: a fake HTTP client serving saved fixtures, so no test touches the network."""
from pathlib import Path

import pytest

from app.scrapers.careers360_nirf import config
from app.scrapers.careers360_nirf.http_client import HttpStatusError
from app.scrapers.careers360_nirf.matcher import SitemapIndex, parse_sitemap
from app.scrapers.careers360_nirf.schemas import Seed, load_parameters

FX = Path(config.FIXTURES_DIR)
BASE = "https://www.careers360.com/university/indian-institute-of-technology-madras"


def fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


class FakeClient:
    """Maps URL -> html (or an exception to raise). Mirrors PoliteClient's public surface."""

    def __init__(self, pages: dict):
        self.pages, self.calls = dict(pages), []
        self.requests_made = 0
        self.blocked_count = 0

    def get(self, url, timeout=None):
        self.calls.append(url)
        self.requests_made += 1
        v = self.pages.get(url)
        if isinstance(v, Exception):
            raise v
        if v is None:
            raise HttpStatusError(url, 404)
        return v


def iitm_pages(all_details: bool = True) -> dict:
    """The fake IIT Madras site. With all_details every course URL serves the saved course page."""
    pages = {
        BASE: fx("iitm_overview.html"),
        f"{BASE}/courses": fx("iitm_courses.html"),
        f"{BASE}/admission": fx("iitm_admission.html"),
        f"{BASE}/courses/be-btech-idpg": fx("iitm_degree_btech_p1.html"),
        f"{BASE}/courses/be-btech-idpg?page=2": fx("iitm_degree_btech_p2.html"),
        f"{BASE}/courses/me-mtech-idpg": fx("iitm_degree_mtech.html"),
        f"{BASE}/courses/phd-idpg": fx("iitm_degree_phd.html"),
        f"{BASE}/courses/mba-idpg": fx("iitm_degree_phd.html"),          # empty listing
        f"{BASE}/btech-electrical-engineering-course": fx("iitm_course_detail.html"),
    }
    if all_details:
        from app.scrapers.careers360_nirf.page_discovery import listing, parse_state
        detail = fx("iitm_course_detail.html")
        for name in ("iitm_degree_btech_p1.html", "iitm_degree_btech_p2.html", "iitm_degree_mtech.html"):
            for row in listing(parse_state(fx(name)))[0]:
                pages.setdefault(f"https://www.careers360.com/{row['course_url']}", detail)
    return pages


def make_seed(**kw) -> Seed:
    base = dict(id="IR-O-U-0456", name="Indian Institute of Technology Madras", city="Chennai",
                state="Tamil Nadu", location="Chennai, Tamil Nadu", score=87.31, rank=1)
    base.update(kw)
    return Seed(**base)


@pytest.fixture
def params():
    return load_parameters()          # the real data/parameters.yaml


@pytest.fixture
def index():
    return SitemapIndex(parse_sitemap(fx("sitemap_sample.xml")))
