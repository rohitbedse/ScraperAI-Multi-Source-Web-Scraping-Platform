"""OPTIONAL live check against careers360.com. Skipped unless CAREERS360_LIVE=1.

Careers360's Terms of Use prohibit automated scraping; only run this if you have permission or
accept that risk. It fetches the robots.txt, the sitemap (cached) and one college.
"""
import os

import pytest

from app.scrapers.careers360_nirf.http_client import PoliteClient
from app.scrapers.careers360_nirf.scraper import Careers360NirfScraper
from app.scrapers.careers360_nirf.schemas import load_parameters, load_seed
from app.scrapers.careers360_nirf.storage import load_records

pytestmark = pytest.mark.skipif(os.environ.get("CAREERS360_LIVE") != "1",
                                reason="live test: set CAREERS360_LIVE=1 (see module docstring)")


def test_live_first_seed_college(tmp_path):
    seeds = load_seed()[:1]
    scraper = Careers360NirfScraper(PoliteClient(), seeds, load_parameters(), out_dir=tmp_path, max_detail_pages=2)
    report = scraper.run()
    rec = load_records(tmp_path / "colleges_data.json")[seeds[0].id]
    assert rec.match.status == "matched", rec.match
    assert rec.college["standard_college_name"]
    assert report["run"]["blocked_responses"] == 0
