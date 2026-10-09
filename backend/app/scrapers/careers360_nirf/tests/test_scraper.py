"""End-to-end orchestration against fixtures (no network): output, report, checkpoint/resume, failures."""
import json

import pytest

from app.scrapers.careers360_nirf.matcher import SitemapIndex
from app.scrapers.careers360_nirf.page_discovery import parse_state

from app.scrapers.careers360_nirf import config
from app.scrapers.careers360_nirf.checkpoint import Checkpoint
from app.scrapers.careers360_nirf.http_client import BlockedError, FetchError
from app.scrapers.careers360_nirf.scraper import Careers360NirfScraper, ProgressEvent, main
from app.scrapers.careers360_nirf.schemas import CollegeRecord, DataFileError
from app.scrapers.careers360_nirf.storage import load_records

from .conftest import BASE, FakeClient, iitm_pages, make_seed

ZZ = make_seed(id="ZZ-1", name="Zzyzx Quantum Academy", city="Nowhere", state="Nowhere", rank=27)
AMB = make_seed(id="AMB-1", name="Example Institute of Technology", city="Mumbai", state="Maharashtra", rank=27)
WRONG_STATE = make_seed(id="WS-1", state="Karnataka", location="Chennai, Karnataka", rank=64)


def build(tmp_path, pages=None, seeds=None, params=None, index=None, **kw):
    client = FakeClient(iitm_pages() if pages is None else pages)
    kw.setdefault("max_detail_pages", 0)             # uncapped unless a test sets a cap
    events = []
    s = Careers360NirfScraper(client, seeds or [make_seed()], params, out_dir=tmp_path, progress=events.append,
                              index=index, **kw)
    return s, client, events


def test_full_college_record(tmp_path, params, index):
    s, client, _ = build(tmp_path, params=params, index=index)
    report = s.run()
    rec = load_records(tmp_path / "colleges_data.json")["IR-O-U-0456"]

    assert rec.status == "scraped" and rec.match.status == "matched"
    assert rec.match.url == BASE and rec.match.state_verified is True and rec.match.city_match is True
    assert rec.seed.rank == 1 and rec.seed.id == "IR-O-U-0456"
    assert rec.college["source"] == "Careers360" and rec.college["url"] == BASE
    assert rec.rankings["nirf_ranking"] == 1 and rec.rankings["qs_world_ranking"] is None
    assert rec.study_destination["study_destination_india"] is True
    assert set(rec.courses) == {"UG", "PG", "PhD"}
    assert len(rec.courses["UG"]) == 17 and len(rec.courses["PG"]) == 14 and rec.courses["PhD"] == []
    assert "courses.PhD" in rec.missing_fields and "qs_world_ranking" in rec.missing_fields
    assert "course.research_areas" in rec.missing_fields
    assert rec.scraped_at

    ee = next(c for c in rec.courses["UG"] if c.fields["course_name"] == "B.Tech Electrical Engineering")
    assert ee.fields["eligibility"] and ee.raw["course_duration"] == "48 Months" and ee.degree == "B.E /B.Tech"

    assert report["match"] == {"matched": 1, "ambiguous": 0, "unmatched": 0}
    assert report["courses_per_level"] == {"UG": 17, "PG": 14, "PhD": 0}
    assert report["parameter_coverage"]["course_level"]["course_name"]["percent"] == 100.0
    assert report["parameter_coverage"]["course_level"]["research_areas"]["filled"] == 0
    assert report["parameter_coverage"]["college_level"]["qs_world_ranking"]["filled"] == 0
    assert report["run"]["requests_made"] == client.requests_made and report["run"]["duration_seconds"] >= 0


def test_duplicates_are_skipped_and_counted(tmp_path, params, index):
    s, _, _ = build(tmp_path, params=params, index=index)
    report = s.run()
    rec = load_records(tmp_path / "colleges_data.json")["IR-O-U-0456"]
    assert rec.duplicates_skipped == 2                 # same id on page 2 + same name/duration under a new id
    assert report["duplicates_skipped"] == 2
    names = [c.fields["course_name"] for c in rec.courses["UG"]]
    assert len(names) == len(set(names))


def test_other_degrees_counted_not_extracted(tmp_path, params, index):
    s, client, _ = build(tmp_path, params=params, index=index)
    report = s.run()
    assert report["courses_skipped_other_level"] == 5           # the combined "B.Tech M.Tech" degree page
    assert not any("btech-mtech-idpg" in u for u in client.calls)       # combined degrees never requested


def test_only_pages_needed_for_yaml_are_requested(tmp_path, params, index):
    slim = params.model_copy(deep=True)
    slim.course_parameters.fields = [f for f in slim.course_fields if f.key in ("course_name", "fees")]
    s, client, _ = build(tmp_path, params=slim, index=index)
    s.run()
    assert f"{BASE}/admission" not in client.calls
    assert not any(u.endswith("-course") for u in client.calls)


def test_detail_page_cap_is_reported_not_hidden(tmp_path, params, index):
    s, client, _ = build(tmp_path, params=params, index=index, max_detail_pages=1)
    s.run()
    rec = load_records(tmp_path / "colleges_data.json")["IR-O-U-0456"]
    assert rec.status == "partial" and rec.course_details_skipped == 30
    assert any("not opened" in e for e in rec.errors)
    assert sum(u.endswith("-course") for u in client.calls) == 1
    unopened = next(c for c in rec.courses["UG"] if c.fields["course_name"] == "B.Tech Civil Engineering")
    assert unopened.fields["eligibility"] is None and "eligibility" in unopened.missing_fields


def test_unmatched_and_ambiguous_are_recorded_not_guessed(tmp_path, params, index):
    s, client, _ = build(tmp_path, seeds=[ZZ, AMB], params=params, index=index)
    report = s.run()
    recs = load_records(tmp_path / "colleges_data.json")
    assert recs["ZZ-1"].status == "unmatched" and recs["ZZ-1"].match.url is None
    assert recs["AMB-1"].status == "ambiguous" and len(recs["AMB-1"].match.candidates) == 2
    assert len(client.calls) == 2                     # only the two tied candidates were probed (404 here)
    assert "profile check: 0 of 2" in recs["AMB-1"].match.reason
    assert report["match"] == {"matched": 0, "ambiguous": 1, "unmatched": 1}
    assert recs["ZZ-1"].seed.rank == 27 and recs["AMB-1"].seed.rank == 27     # tied ranks untouched


def _relocated(html: str, city: str, state: str) -> str:
    st = parse_state(html)
    st["commonCollegeData"]["headerDetail"]["institution_data"]["current_location"] = {
        "city_name": city, "state_name": state}
    return "<script>window.INITIAL_STATE=" + json.dumps(st) + "</script>"


def _tied_index():
    return SitemapIndex([BASE, BASE + "-2"])         # two near-identical profile names


def test_tie_is_resolved_only_by_profile_city_and_state_evidence(tmp_path, params):
    pages = iitm_pages()
    pages[BASE + "-2"] = _relocated(pages[BASE], "Bengaluru", "Karnataka")
    s, client, _ = build(tmp_path, pages=pages, params=params, index=_tied_index())
    s.run()
    rec = load_records(tmp_path / "colleges_data.json")["IR-O-U-0456"]
    assert rec.status == "scraped" and rec.match.url == BASE
    assert rec.match.status == "matched" and "only this candidate" in rec.match.reason
    assert client.calls.count(BASE) == 1                  # the probed page is reused, not fetched twice


def test_tie_stays_ambiguous_when_both_or_neither_profile_agree(tmp_path, params):
    both = iitm_pages()
    both[BASE + "-2"] = both[BASE]                      # both claim Chennai, Tamil Nadu
    s, client, _ = build(tmp_path, pages=both, params=params, index=_tied_index())
    s.run()
    rec = load_records(tmp_path / "colleges_data.json")["IR-O-U-0456"]
    assert rec.status == "ambiguous" and rec.match.url is None and "2 of 2" in rec.match.reason
    neither = iitm_pages()
    neither[BASE] = _relocated(neither[BASE], "Pune", "Maharashtra")
    neither[BASE + "-2"] = _relocated(neither[BASE], "Pune", "Maharashtra")
    s2, _, _ = build(tmp_path, pages=neither, params=params, index=_tied_index())
    s2.run()
    assert load_records(tmp_path / "colleges_data.json")["IR-O-U-0456"].status == "ambiguous"


def test_large_ties_are_not_probed(tmp_path, params):
    urls = [f"https://www.careers360.com/university/amity-university-{c}" for c in
            ("noida", "pune", "jaipur", "gurugram", "lucknow")]
    seed = make_seed(name="Amity University", city="Delhi", state="Delhi")      # no slug names Delhi
    s, client, _ = build(tmp_path, pages={}, seeds=[seed], params=params, index=SitemapIndex(urls))
    s.run()
    rec = next(iter(load_records(tmp_path / "colleges_data.json").values()))
    assert rec.status == "ambiguous" and len(rec.match.candidates) == 5
    assert client.calls == []                         # too many near-ties to probe: nothing fetched


def test_state_mismatch_downgrades_match_to_ambiguous(tmp_path, params, index):
    s, _, _ = build(tmp_path, seeds=[WRONG_STATE], params=params, index=index)
    s.run()
    rec = load_records(tmp_path / "colleges_data.json")["WS-1"]
    assert rec.status == "ambiguous" and rec.match.state_verified is False and rec.college == {}


def test_missing_course_page_gives_partial_with_nulls(tmp_path, params, index):
    pages = iitm_pages()
    del pages[f"{BASE}/courses"]
    s, _, _ = build(tmp_path, pages=pages, params=params, index=index)
    s.run()
    rec = load_records(tmp_path / "colleges_data.json")["IR-O-U-0456"]
    assert rec.status == "partial" and any("courses" in e for e in rec.errors)
    assert rec.college["standard_college_name"] and all(v == [] for v in rec.courses.values())


def test_failing_course_pages_are_summarized_and_make_college_partial(tmp_path, params, index):
    s, _, _ = build(tmp_path, pages=iitm_pages(all_details=False), params=params, index=index)
    s.run()
    rec = load_records(tmp_path / "colleges_data.json")["IR-O-U-0456"]
    assert rec.status == "partial"
    assert len(rec.errors) <= 5 and any("more course pages failed" in e for e in rec.errors)
    assert next(c for c in rec.courses["UG"] if c.fields["course_name"] == "B.Tech Electrical Engineering"
                ).fields["eligibility"]                                      # the one reachable page still used


def test_failing_degree_listing_makes_college_partial_not_lost(tmp_path, params, index):
    pages = iitm_pages()
    del pages[f"{BASE}/courses/me-mtech-idpg"]               # 404 on the PG listing
    s, _, _ = build(tmp_path, pages=pages, params=params, index=index)
    s.run()
    rec = load_records(tmp_path / "colleges_data.json")["IR-O-U-0456"]
    assert rec.status == "partial" and any("M.E /M.Tech." in e for e in rec.errors)
    assert len(rec.courses["UG"]) == 17 and rec.courses["PG"] == []


def test_profile_404_fails_college_without_stopping_run(tmp_path, params, index):
    pages = iitm_pages()
    del pages[BASE]
    s, _, _ = build(tmp_path, pages=pages, seeds=[make_seed(), ZZ], params=params, index=index)
    report = s.run()
    recs = load_records(tmp_path / "colleges_data.json")
    assert recs["IR-O-U-0456"].status == "failed" and recs["ZZ-1"].status == "unmatched"
    assert report["scrape"]["failed"] == 1 and report["failed_ids"] == ["IR-O-U-0456"]


def test_layout_change_is_a_clear_failure(tmp_path, params, index):
    pages = iitm_pages()
    pages[BASE] = "<html>redesigned, no embedded state</html>"
    s, _, _ = build(tmp_path, pages=pages, params=params, index=index)
    s.run()
    rec = load_records(tmp_path / "colleges_data.json")["IR-O-U-0456"]
    assert rec.status == "failed" and "INITIAL_STATE" in rec.errors[0]


def test_blocked_mid_college_stops_that_college_and_is_logged(tmp_path, params, index, caplog):
    pages = iitm_pages()
    pages[f"{BASE}/courses/me-mtech-idpg"] = BlockedError("HTTP 429")
    s, _, _ = build(tmp_path, pages=pages, seeds=[make_seed(), ZZ], params=params, index=index)
    s.run()
    recs = load_records(tmp_path / "colleges_data.json")
    assert recs["IR-O-U-0456"].status == "failed" and recs["IR-O-U-0456"].errors[-1].startswith("BLOCKED")
    assert recs["ZZ-1"].status == "unmatched"          # the run continued with the next college


def test_run_aborts_after_repeated_blocks(tmp_path, params, index, monkeypatch):
    monkeypatch.setattr(config, "MAX_CONSECUTIVE_BLOCKED_COLLEGES", 2)
    pages = {BASE: BlockedError("HTTP 403")}
    seeds = [make_seed(id=f"S{i}") for i in range(5)]
    s, client, events = build(tmp_path, pages=pages, seeds=seeds, params=params, index=index)
    report = s.run()
    assert report["run"]["aborted"] and "blocked" in report["run"]["aborted"]
    assert len(client.calls) == 2 and events[-1].stage == "aborted"


def test_unexpected_exception_does_not_kill_the_run(tmp_path, params, index, monkeypatch):
    s, _, _ = build(tmp_path, seeds=[make_seed(), ZZ], params=params, index=index)
    real = s.scrape_college
    monkeypatch.setattr(s, "scrape_college",
                        lambda seed: (_ for _ in ()).throw(RuntimeError("boom")) if seed.id != "ZZ-1" else real(seed))
    s.run()
    recs = load_records(tmp_path / "colleges_data.json")
    assert recs["IR-O-U-0456"].status == "failed" and "boom" in recs["IR-O-U-0456"].errors[0]
    assert recs["ZZ-1"].status == "unmatched"


# ------------------------------------------------------------------------- resume
def test_resume_skips_finished_and_retries_failed(tmp_path, params, index):
    pages = iitm_pages()
    broken = dict(pages)
    broken[BASE] = FetchError("flaky network")
    seeds = [make_seed(), ZZ]

    s1, _, _ = build(tmp_path, pages=broken, seeds=seeds, params=params, index=index)
    s1.run()
    cp = Checkpoint.load(tmp_path / "checkpoint.json")
    assert cp.failed_ids() == ["IR-O-U-0456"] and cp.is_done("ZZ-1") and not cp.is_done("IR-O-U-0456")

    s2, client2, _ = build(tmp_path, pages=pages, seeds=seeds, params=params, index=index)
    report = s2.run(resume=True)
    assert BASE in client2.calls                                   # failed college retried
    assert report["run"]["skipped_resume"] == 1 and report["run"]["processed_this_run"] == 1
    recs = load_records(tmp_path / "colleges_data.json")
    assert recs["IR-O-U-0456"].status == "scraped" and recs["ZZ-1"].status == "unmatched"
    assert Checkpoint.load(tmp_path / "checkpoint.json").entries["IR-O-U-0456"]["attempts"] == 2

    s3, client3, _ = build(tmp_path, pages=pages, seeds=seeds, params=params, index=index)
    s3.run(resume=True)
    assert client3.calls == []                                     # everything finished: nothing fetched


def test_without_resume_everything_is_redone_but_other_records_kept(tmp_path, params, index):
    s1, _, _ = build(tmp_path, seeds=[make_seed(), ZZ], params=params, index=index)
    s1.run()
    s2, client2, _ = build(tmp_path, seeds=[make_seed(), ZZ], params=params, index=index)
    s2.run(only=["ZZ-1"])
    assert set(load_records(tmp_path / "colleges_data.json")) == {"IR-O-U-0456", "ZZ-1"}
    assert client2.calls == []


def test_only_and_limit_selection(tmp_path, params, index):
    seeds = [ZZ, make_seed(), AMB]
    s, _, _ = build(tmp_path, seeds=seeds, params=params, index=index)
    s.run(limit=2)
    assert set(load_records(tmp_path / "colleges_data.json")) == {"ZZ-1", "IR-O-U-0456"}
    with pytest.raises(DataFileError, match="unknown seed id"):
        s.run(only=["nope"])


# ------------------------------------------------------------------ output + progress
def test_outputs_are_valid_json_and_atomic(tmp_path, params, index):
    s, _, _ = build(tmp_path, params=params, index=index)
    s.run()
    data = json.loads((tmp_path / "colleges_data.json").read_text(encoding="utf-8"))
    CollegeRecord.model_validate(data[0])                              # Pydantic-validated shape
    assert {"seed", "match", "college", "courses", "rankings", "study_destination", "missing_fields",
            "scraped_at"} <= set(data[0])
    assert not list(tmp_path.glob("*.tmp"))                            # no temp files left behind
    assert (tmp_path / "scrape_report.json").is_file() and (tmp_path / "checkpoint.json").is_file()


def test_corrupt_existing_output_is_an_error_not_silently_overwritten(tmp_path, params, index):
    (tmp_path / "colleges_data.json").write_text("{broken")
    s, _, _ = build(tmp_path, params=params, index=index)
    with pytest.raises(DataFileError, match="unreadable"):
        s.run()
    assert (tmp_path / "colleges_data.json").read_text() == "{broken"


def test_progress_events(tmp_path, params, index):
    s, _, events = build(tmp_path, seeds=[make_seed(), ZZ, AMB], params=params, index=index)
    s.run()
    assert all(isinstance(e, ProgressEvent) for e in events)
    assert events[0].stage == "starting" and events[0].pending == 3
    last = events[-1]
    assert last.stage == "done" and last.processed == 3 and last.total == 3 and last.pending == 0
    assert (last.matched, last.unmatched, last.ambiguous, last.failed) == (1, 1, 1, 0)
    assert last.courses_extracted == 31
    assert {"matching", "profile", "courses", "admission", "course_details", "saving"} <= {e.stage for e in events}


# --------------------------------------------------------------------------- CLI
def test_cli_refuses_network_without_allow_live(capsys):
    assert main([]) == 2
    err = capsys.readouterr().err
    assert "--allow-live" in err and "Terms of Use" in err
