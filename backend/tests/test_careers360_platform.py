"""Platform wrapper for Careers360 NIRF: registry, /sources, params, stages, progress, errors, cancel, resume,
download. All HTTP is mocked with the saved IIT Madras fixtures; nothing touches the network."""
import io
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.scrapers.careers360_nirf import platform_scraper as ps
from app.scrapers.careers360_nirf.checkpoint import Checkpoint
from app.scrapers.careers360_nirf.http_client import BlockedError, FetchError
from app.scrapers.careers360_nirf.matcher import SitemapIndex, parse_sitemap
from app.scrapers.careers360_nirf.tests.conftest import BASE, FakeClient, fx, iitm_pages, make_seed
from app.scrapers.registry import REGISTRY, describe_sources, get_scraper_class

BACKEND = Path(__file__).resolve().parent.parent
IITM = make_seed()
ZZ = make_seed(id="ZZ-1", name="Zzyzx Quantum Academy", city="Nowhere", state="Nowhere", rank=27)
FIRST_SEED_ID = "IR-O-U-0456"


@pytest.fixture
def params_file():
    from app.scrapers.careers360_nirf.schemas import load_parameters
    return load_parameters()


def build(tmp_path, monkeypatch, params=None, pages=None, seeds=None):
    seeds = seeds or [IITM]
    monkeypatch.setattr(ps, "load_seed", lambda: seeds)
    out = io.StringIO()
    sc = ps.Careers360NirfPlatformScraper(uuid.uuid4().hex, params or {}, out=out)
    sc.work_dir = tmp_path
    sc.index = SitemapIndex(parse_sitemap(fx("sitemap_sample.xml")))
    client = FakeClient(iitm_pages() if pages is None else pages)
    monkeypatch.setattr(sc, "make_client", lambda: client)
    return sc, client, out


def events(out):
    return [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]


def errors(out):
    return [e for e in events(out) if e["event_type"] == "error"]


# ------------------------------------------------------------------------------------------- registry
def test_registry_lists_scraper_and_keeps_existing_entries():
    assert REGISTRY["careers360_nirf"].endswith("platform_scraper:Careers360NirfPlatformScraper")
    assert REGISTRY["swayam"] == "app.scrapers.swayam.scraper:SwayamScraper"
    assert REGISTRY["mindler"] == "app.scrapers.mindler.scraper:MindlerScraper"
    cls = get_scraper_class("careers360_nirf")
    assert cls.id == "careers360_nirf" and cls.name == "Careers360 – NIRF Top 100 Colleges"
    assert cls.max_concurrent == 1 and cls.description
    assert [s.value for s in cls.stages] == ["INIT", "FETCH_LIST", "FETCH_DETAIL", "PARSE", "VALIDATE",
                                              "DEDUPE", "SAVE", "DONE"]
    assert get_scraper_class("swayam").max_concurrent == 1 and get_scraper_class("mindler").max_concurrent == 2


def test_sources_endpoint_lists_it_as_available(client):
    src = {s["id"]: s for s in client.get("/sources").json()}
    c = src["careers360_nirf"]
    assert c["status"] == "available" and c["max_concurrent"] == 1
    assert set(c["params_schema"]["properties"]) == {"limit", "only", "resume"}
    assert src["swayam"]["status"] == "available" and src["mindler"]["status"] == "available"


def test_import_failure_is_reported_with_a_reason(monkeypatch):
    monkeypatch.setitem(REGISTRY, "broken", "app.scrapers.does_not_exist:Nope")
    broken = next(s for s in describe_sources() if s["id"] == "broken")
    assert broken["status"] == "unavailable"
    assert "ModuleNotFoundError" in broken["reason"] and "does_not_exist" in broken["reason"]


# ------------------------------------------------------------------------------------------- params
def test_params_validation():
    P = ps.Careers360Params
    assert P().limit == 0 and P().only == [] and P().resume is False
    assert P(limit=100).limit == 100 and P(only=[FIRST_SEED_ID]).only == [FIRST_SEED_ID]
    for bad in ({"limit": -1}, {"limit": 101}, {"limit": "many"}, {"resume": "maybe"}, {"only": "IR-O-U-0456"},
                {"only": ["NOT-A-REAL-ID"]}, {"only": [FIRST_SEED_ID, FIRST_SEED_ID]}, {"colour": "red"}):
        with pytest.raises(ValidationError):
            P(**bad)


def test_api_rejects_bad_params(client):
    for params in ({"limit": 500}, {"nope": 1}, {"only": ["bogus"]}):
        assert client.post("/jobs", json={"source": "careers360_nirf", "params": params}).status_code == 422


# ------------------------------------------------------------------------------------------- happy path
def test_job_runs_through_all_stages_and_writes_output(tmp_path, monkeypatch):
    sc, client, out = build(tmp_path, monkeypatch, seeds=[IITM, ZZ])
    assert sc.execute() == 0
    evs = events(out)
    stages = [e["stage"] for e in evs if e["event_type"] == "stage"]
    assert stages == ["INIT", "FETCH_LIST", "FETCH_DETAIL", "PARSE", "VALIDATE", "DEDUPE", "SAVE"]
    done = evs[-1]
    assert done["event_type"] == "done" and done["stage"] == "DONE" and done["output_file"] == "result.json"
    assert done["stats"]["colleges"] == 2 and done["stats"]["matched"] == 1 and done["stats"]["unmatched"] == 1
    result = json.loads(sc.output_path.read_text(encoding="utf-8"))
    assert result["total_items"] == 2 and [i["seed"]["id"] for i in result["items"]] == [FIRST_SEED_ID, "ZZ-1"]
    assert sum(len(v) for v in result["items"][0]["courses"].values()) == done["stats"]["courses_extracted"] > 0
    # CLI artefacts kept: work folder + copies beside the job output
    for name in ("colleges_data.json", "scrape_report.json", "checkpoint.json", "scraper.log"):
        assert (tmp_path / name).is_file(), name
    for name in ("colleges_data.json", "scrape_report.json"):
        assert (sc.output_path.parent / name).is_file()
    assert not list(tmp_path.glob("*.tmp"))


def test_progress_events_carry_real_counts(tmp_path, monkeypatch):
    sc, client, out = build(tmp_path, monkeypatch, seeds=[IITM, ZZ])
    sc.execute()
    detail = [e for e in events(out) if e["stage"] == "FETCH_DETAIL" and e.get("data")]
    assert detail, "no FETCH_DETAIL events with data"
    first, last = detail[0]["data"], detail[-1]["data"]
    assert first["total"] == 2 and last["total"] == 2
    assert last["processed"] == 2 and last["pending"] == 0
    assert (last["matched"], last["unmatched"], last["failed"]) == (1, 1, 0)
    assert last["courses_extracted"] > 0
    assert any(e["data"]["current_college"] == IITM.name for e in detail)
    assert any(e["data"]["pending"] == 1 and e["data"]["processed"] == 1 for e in detail)   # mid-run, not simulated
    counts = [e["data"]["courses_extracted"] for e in detail]
    assert counts == sorted(counts)                                                          # only ever grows
    assert any(e["items_total"] == 2 for e in detail)


def test_limit_and_only_select_colleges(tmp_path, monkeypatch):
    sc, client, out = build(tmp_path, monkeypatch, params={"only": ["ZZ-1"]}, seeds=[IITM, ZZ])
    assert sc.execute() == 0
    assert json.loads(sc.output_path.read_text())["total_items"] == 1 and client.calls == []
    sc, client, out = build(tmp_path / "b", monkeypatch, params={"limit": 1}, seeds=[ZZ, IITM])
    assert sc.execute() == 0
    assert [i["seed"]["id"] for i in json.loads(sc.output_path.read_text())["items"]] == ["ZZ-1"]


# ------------------------------------------------------------------------------------------- errors
def test_one_failed_college_does_not_stop_the_job(tmp_path, monkeypatch):
    pages = iitm_pages()
    pages[BASE] = FetchError("https://x: failed after 4 attempts (Read timed out)")
    sc, client, out = build(tmp_path, monkeypatch, pages=pages, seeds=[IITM, ZZ])
    assert sc.execute() == 0                                           # partially completed, not failed
    evs = events(out)
    assert evs[-1]["stage"] == "PARTIALLY_COMPLETED"
    err = errors(out)[0]
    assert err["error_code"] == "NETWORK_TIMEOUT" and err["error_type"] == "FetchError"
    assert err["stage"] == "FETCH_DETAIL" and err["job_continues"] is True and err["retryable"] is True
    assert "Traceback" in err["traceback"] and "FetchError" in err["traceback"]
    assert evs[-1]["stats"]["failed"] == 1 and evs[-1]["stats"]["unmatched"] == 1
    items = {i["seed"]["id"]: i for i in json.loads(sc.output_path.read_text())["items"]}
    assert items[FIRST_SEED_ID]["status"] == "failed" and items["ZZ-1"]["status"] == "unmatched"
    assert Checkpoint.load(tmp_path / "checkpoint.json").failed_ids() == [FIRST_SEED_ID]


def test_unexpected_exception_is_captured_with_traceback(tmp_path, monkeypatch):
    sc, client, out = build(tmp_path, monkeypatch, seeds=[IITM, ZZ])
    import app.scrapers.careers360_nirf.scraper as core_mod
    real = core_mod.Careers360NirfScraper.scrape_college

    def flaky(self, seed):
        if seed.id == FIRST_SEED_ID:
            raise RuntimeError("boom")
        return real(self, seed)
    monkeypatch.setattr(core_mod.Careers360NirfScraper, "scrape_college", flaky)
    assert sc.execute() == 0
    err = errors(out)[0]
    assert err["error_code"] == "SCRAPER_ERROR" and err["error_type"] == "RuntimeError"
    assert "boom" in err["technical_message"] and "Traceback" in err["traceback"]


@pytest.mark.parametrize("message,code", [("HTTP 429 for https://x", "BLOCKED_429"),
                                          ("HTTP 403 for https://x", "BLOCKED_403")])
def test_fully_blocked_run_fails_with_the_right_code(tmp_path, monkeypatch, message, code):
    pages = iitm_pages()
    pages[BASE] = BlockedError(message)
    sc, client, out = build(tmp_path, monkeypatch, pages=pages)
    assert sc.execute() == 1
    errs = errors(out)
    assert errs[0]["error_code"] == code and errs[0]["error_type"] == "BlockedError"
    fatal = errs[-1]
    assert fatal["error_code"] == code and fatal["job_continues"] is False
    assert "blocked" in fatal["message"].lower() and "not being worked around" in fatal["message"]
    assert events(out)[-1]["stage"] == "FAILED"
    assert client.calls.count(BASE) == 1                                # no retry loop around the block


def test_repeated_blocks_abort_the_run(tmp_path, monkeypatch):
    pages = {BASE: BlockedError("HTTP 429 for x")}
    seeds = [make_seed(id=f"S{i}") for i in range(6)]
    sc, client, out = build(tmp_path, monkeypatch, pages=pages, seeds=seeds)
    assert sc.execute() == 1
    assert errors(out)[-1]["error_code"] == "BLOCKED_429"
    assert len(client.calls) == 3                                       # stopped after 3 blocked colleges in a row
    assert not sc.output_path.exists() or json.loads(sc.output_path.read_text())["total_items"] == 0


def test_block_after_progress_keeps_finished_colleges(tmp_path, monkeypatch):
    pages = iitm_pages()
    pages[f"{BASE}/courses/me-mtech-idpg"] = BlockedError("HTTP 429 for x")
    sc, client, out = build(tmp_path, monkeypatch, pages=pages, seeds=[IITM, ZZ])
    assert sc.execute() == 0
    assert any(e["error_code"] == "BLOCKED_429" and e["job_continues"] for e in errors(out))
    assert events(out)[-1]["stage"] == "PARTIALLY_COMPLETED"


def test_live_access_requires_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv(ps.ALLOW_LIVE_ENV, raising=False)
    monkeypatch.setattr(ps, "load_seed", lambda: [IITM])
    out = io.StringIO()
    sc = ps.Careers360NirfPlatformScraper(uuid.uuid4().hex, {}, out=out)
    sc.work_dir = tmp_path
    assert sc.execute() == 1
    assert ps.ALLOW_LIVE_ENV in errors(out)[-1]["message"]


@pytest.mark.parametrize("exc,code", [
    (BlockedError("HTTP 429 x"), "BLOCKED_429"), (BlockedError("captcha page"), "BLOCKED_403"),
    (FetchError("x: failed (Read timed out)"), "NETWORK_TIMEOUT"), (FetchError("x: failed (refused)"), "HTTP_ERROR"),
    (ps.LayoutChanged("window.INITIAL_STATE not found"), "LAYOUT_CHANGED"),
    (ps.LayoutChanged("INITIAL_STATE is not valid JSON: x"), "PARSE_ERROR"),
])
def test_error_code_mapping(exc, code):
    assert ps.map_error_code(exc, ps.Stage.FETCH_DETAIL).value == code


# ------------------------------------------------------------------------------------------- cancel / resume
def test_cancel_stops_between_colleges_and_flushes(tmp_path, monkeypatch):
    sc, client, out = build(tmp_path, monkeypatch, seeds=[IITM, make_seed(id="ZZ-1", name="Zzyzx Quantum Academy",
                                                                           city="Nowhere", state="Nowhere")])
    real = sc._after_college

    def cancel_after_first(seed_id):
        real(seed_id)
        sc.request_cancel()                                             # what the worker's SIGTERM handler does
    monkeypatch.setattr(sc, "_after_college", cancel_after_first)
    assert sc.execute() == 3
    assert errors(out)[-1]["error_code"] == "CANCELLED"
    cp = Checkpoint.load(tmp_path / "checkpoint.json")
    assert cp.is_done(FIRST_SEED_ID) and not cp.is_done("ZZ-1")        # the finished college is safe, the next never started
    saved = json.loads((tmp_path / "colleges_data.json").read_text())
    assert [r["seed"]["id"] for r in saved] == [FIRST_SEED_ID]
    partial = json.loads(sc.output_path.read_text())                    # partial output flushed
    assert partial["total_items"] == 1 and partial["cancelled"] is True
    assert not list(tmp_path.glob("*.tmp"))


def test_cancel_in_the_middle_of_a_college(tmp_path, monkeypatch):
    sc, client, out = build(tmp_path, monkeypatch)
    real_get = client.get

    def get(url, timeout=None):
        if url.endswith("/courses"):
            sc.request_cancel()
        return real_get(url, timeout)
    client.get = get
    assert sc.execute() == 3
    # the in-flight college is not half-recorded: it will be redone on resume
    assert not Checkpoint.load(tmp_path / "checkpoint.json").is_done(FIRST_SEED_ID)
    assert client.calls[-1].endswith("/courses")                        # no further pages were requested


def test_resume_uses_the_existing_checkpoint(tmp_path, monkeypatch):
    broken = iitm_pages()
    broken[BASE] = FetchError("flaky network")
    sc1, _, out1 = build(tmp_path, monkeypatch, pages=broken, seeds=[IITM, ZZ])
    assert sc1.execute() == 0
    sc2, client2, out2 = build(tmp_path, monkeypatch, params={"resume": True}, seeds=[IITM, ZZ])
    assert sc2.execute() == 0
    assert sc2._this_run == [FIRST_SEED_ID]                             # ZZ-1 was skipped, the failed one retried
    last = events(out2)[-1]
    assert last["stage"] == "DONE" and last["stats"]["failed"] == 0 and last["stats"]["matched"] == 1
    assert json.loads(sc2.output_path.read_text())["total_items"] == 2  # resumed college still in the output
    first_detail = next(e for e in events(out2) if e["stage"] == "FETCH_DETAIL" and e.get("data"))
    assert first_detail["data"]["processed"] >= 1 and first_detail["data"]["total"] == 2
    cp = Checkpoint.load(tmp_path / "checkpoint.json")
    assert cp.is_done(FIRST_SEED_ID) and cp.entries[FIRST_SEED_ID]["attempts"] == 2


# ------------------------------------------------------------------------------------------- API / paths
def test_download_returns_the_job_output(client, tmp_path, monkeypatch):
    from app import config
    from app.db.models import Job
    from app.db.session import session_scope
    sc, _, _ = build(tmp_path, monkeypatch)
    assert sc.execute() == 0
    with session_scope() as s:
        s.add(Job(id=sc.job_id, source="careers360_nirf", status="completed", params="{}", output_path="result.json"))
    r = client.get(f"/jobs/{sc.job_id}/download")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "careers360_nirf" and body["items"][0]["seed"]["id"] == FIRST_SEED_ID
    assert (config.job_dir(sc.job_id) / "output" / "colleges_data.json").is_file()


def test_job_without_live_opt_in_fails_cleanly_through_the_api(client, monkeypatch):
    """Real worker subprocess, no mocks: proves the registry/worker wiring and the clear failure reason."""
    from tests.conftest import wait_done
    monkeypatch.delenv(ps.ALLOW_LIVE_ENV, raising=False)
    r = client.post("/jobs", json={"source": "careers360_nirf", "params": {"limit": 1}})
    assert r.status_code == 201
    j = wait_done(client, r.json()["id"], timeout=60)
    assert j["status"] == "failed"
    assert ps.ALLOW_LIVE_ENV in j["errors"][-1]["message"] and j["errors"][-1]["job_continues"] is False


def test_data_files_are_found_from_any_working_directory(tmp_path):
    code = ("from app.scrapers.careers360_nirf.schemas import load_seed, load_parameters; "
            "print(len(load_seed()), bool(load_parameters()))")
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True,
                       env={**__import__("os").environ, "PYTHONPATH": str(BACKEND)})
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["100", "True"]
