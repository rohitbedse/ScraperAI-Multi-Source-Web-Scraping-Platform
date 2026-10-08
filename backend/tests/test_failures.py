"""Failure-path tests: real exception captured, right stage + code, traceback only in logs/details,
job never stuck, partial data preserved, retry works, no orphan Chromium."""
import io
import json
import os
import subprocess
import sys
import time

import pytest

from tests import fakes
from tests.conftest import wait_done


# ----------------------------------------------------------------------------- helpers
def post(client, source, **params):
    r = client.post("/jobs", json={"source": source, "params": params})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def errors(client, jid):
    return client.get(f"/jobs/{jid}?include_traceback=true").json()["errors"]


def log_text(jid):
    from app import config
    return (config.job_dir(jid) / "logs" / "job.log").read_text(encoding="utf-8", errors="replace")


def wait_status(client, jid, status, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if client.get(f"/jobs/{jid}").json()["status"] == status:
            return
        time.sleep(0.1)
    raise AssertionError(f"never reached {status}")


def playwright_pids() -> set:
    """Chromium processes launched from the ms-playwright folder (ours, not the user's Chrome)."""
    if os.name == "nt":
        cmd = ["powershell", "-NoProfile", "-Command",
               "(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like '*ms-playwright*' })"
               ".ProcessId"]
    else:
        cmd = ["pgrep", "-f", "ms-playwright"]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    return {int(x) for x in out.split() if x.isdigit()}


def assert_friendly(job):
    """Main UI message never contains a traceback."""
    assert "Traceback" not in job["message"]
    for e in job["errors"]:
        assert e["traceback"] is None and "Traceback" not in e["message"]


# ----------------------------------------------------------------------------- Mindler
@pytest.fixture()
def mindler(monkeypatch):
    from app import config
    for f in config.STATE_DIR.glob("mindler_career_library.json*"):
        f.unlink()
    srv = fakes.mindler_server()
    monkeypatch.setenv("MINDLER_DOMAIN_LIST_URL", srv.base + "/list")
    monkeypatch.setenv("MINDLER_DOMAIN_DETAILS_URL", srv.base + "/details")
    monkeypatch.setenv("MINDLER_TIMEOUT", "1.5")
    yield srv
    srv.close()


def test_mindler_happy_path_updates_master(client, mindler):
    from app import config
    job = wait_done(client, post(client, "mindler"))
    assert job["status"] == "completed" and job["stats"]["duplicates"] == 3   # 1 dup per domain
    assert [json.loads(l)["subject_id"] for l in
            (config.job_dir(job["id"]) / "partial" / "items.jsonl").read_text().splitlines()] \
        == ["engineering", "medical", "bad"]
    assert (config.STATE_DIR / "mindler_career_library.json").exists()


def test_mindler_429(client, mindler):
    mindler.mode = "429"
    jid = post(client, "mindler")
    job = wait_done(client, jid)
    assert job["status"] == "failed" and not job["has_output"]
    e = errors(client, jid)[-1]
    assert (e["error_code"], e["stage"]) == ("BLOCKED_429", "FETCH_DETAIL")
    assert e["error_type"] == "HTTPError" and e["technical_message"] == "HTTP 429 Too Many Requests"
    assert e["retryable"] and not e["job_continues"]
    assert "Traceback" in e["traceback"] and "429" in e["traceback"]
    assert "Traceback" in log_text(jid)                       # full traceback is in the server log
    assert_friendly(client.get(f"/jobs/{jid}").json())
    # retry works once the site recovers; the master dataset was never touched by the failed job
    from app import config
    assert not (config.STATE_DIR / "mindler_career_library.json").exists()
    mindler.mode = "ok"
    new = client.post(f"/jobs/{jid}/rerun").json()["id"]
    assert wait_done(client, new)["status"] == "completed"


def test_mindler_timeout(client, mindler):
    mindler.mode = "timeout"
    jid = post(client, "mindler")
    job = wait_done(client, jid, timeout=60)
    e = errors(client, jid)[-1]
    assert job["status"] == "failed"
    assert (e["error_code"], e["stage"], e["retryable"]) == ("NETWORK_TIMEOUT", "FETCH_DETAIL", True)
    assert "Timeout" in e["error_type"] and "Traceback" in e["traceback"]


def test_mindler_malformed_json(client, mindler):
    mindler.mode = "garbage"
    jid = post(client, "mindler")
    job = wait_done(client, jid)
    e = errors(client, jid)[-1]
    assert job["status"] == "failed"
    assert (e["error_code"], e["stage"], e["retryable"]) == ("PARSE_ERROR", "FETCH_DETAIL", False)


def test_mindler_malformed_list(client, mindler):
    mindler.mode = "list_badshape"
    jid = post(client, "mindler")
    job = wait_done(client, jid)
    e = errors(client, jid)[-1]
    assert job["status"] == "failed" and (e["error_code"], e["stage"]) == ("PARSE_ERROR", "FETCH_LIST")


def test_mindler_bad_shape_keeps_previous_data(client, mindler):
    from app import config
    assert wait_done(client, post(client, "mindler"))["status"] == "completed"
    master = config.STATE_DIR / "mindler_career_library.json"
    before = json.loads(master.read_text())
    mindler.mode = "badshape"                                  # only the "bad" domain is malformed
    jid = post(client, "mindler")
    job = wait_done(client, jid)
    assert job["status"] == "partially_completed" and job["has_output"]
    e = errors(client, jid)[-1]
    assert (e["error_code"], e["stage"], e["job_continues"]) == ("PARSE_ERROR", "PARSE", True)
    items = {i["subject_id"]: i for i in client.get(f"/jobs/{jid}/download").json()["items"]}
    assert items["bad"] == next(r for r in before if r["subject_id"] == "bad")    # previous record preserved
    assert json.loads(master.read_text()) == before or len(json.loads(master.read_text())) == len(before)


def test_mindler_cancel(client, mindler):
    from app import config
    mindler.mode = "slow"
    jid = post(client, "mindler")
    time.sleep(2.5)
    assert client.post(f"/jobs/{jid}/cancel").status_code == 200
    assert wait_done(client, jid)["status"] == "cancelled"
    raw = config.job_dir(jid) / "partial" / "raw.jsonl"
    assert raw.exists() and raw.read_text().strip()             # partial progress kept
    assert not (config.STATE_DIR / "mindler_career_library.json").exists()   # master untouched
    assert not job_is_running(client, jid)


def job_is_running(client, jid):
    return client.get(f"/jobs/{jid}").json()["status"] in ("running", "cancelling", "queued")


# ----------------------------------------------------------------------------- SWAYAM
@pytest.fixture()
def swayam(monkeypatch):
    from app import config
    from app.scrapers.swayam import core
    srv = fakes.swayam_server()
    monkeypatch.setenv("SWAYAM_EXPLORER", srv.base + "/explorer")
    monkeypatch.setenv("SWAYAM_PAGE_TIMEOUT_MS", "2500")
    # saved course list (the explorer crawl itself is covered by test_swayam_crawl)
    cards = [core.ExplorerCard(url=f"{srv.base}/c/{n}/preview", card_name=f"Course {n}", source="NPTEL",
                               card_duration="4 Weeks", explorer_tab="Upcoming").model_dump() for n in range(1, 5)]
    (config.STATE_DIR / "swayam_cards.json").write_text(json.dumps(cards))
    for f in config.STATE_DIR.glob("swayam_progress.jsonl"):
        f.unlink()
    yield srv
    srv.close()


SW = {"reuse_cards": True, "fresh": True, "workers": 2}


def test_swayam_happy_and_resume(client, swayam):
    job = wait_done(client, post(client, "swayam", **SW), timeout=90)
    assert job["status"] == "completed" and job["stats"]["items"] == 4
    swayam.hang.add(1)                         # a resumed run must not re-open cached pages
    job2 = wait_done(client, post(client, "swayam", reuse_cards=True, workers=2), timeout=90)
    assert job2["status"] == "completed", job2


def test_swayam_page_timeout(client, swayam):
    swayam.hang.add(2)
    jid = post(client, "swayam", **SW)
    job = wait_done(client, jid, timeout=90)
    assert job["status"] == "partially_completed" and job["stats"]["items"] == 4   # card-only row kept
    e = [x for x in errors(client, jid) if x["error_code"] == "NETWORK_TIMEOUT"][0]
    assert (e["stage"], e["retryable"], e["job_continues"]) == ("FETCH_DETAIL", True, True)
    assert "Timeout" in e["error_type"] and "Traceback" in e["traceback"] and "Traceback" in log_text(jid)
    assert_friendly(client.get(f"/jobs/{jid}").json())
    swayam.hang.clear()                       # retry succeeds
    assert wait_done(client, client.post(f"/jobs/{jid}/rerun").json()["id"], timeout=90)["status"] == "completed"


def test_swayam_validation_failure(client, swayam):
    swayam.blank.add(3)                       # course with no name anywhere (page AND card) -> unusable row
    from app import config
    f = config.STATE_DIR / "swayam_cards.json"
    cards = json.loads(f.read_text())
    cards[2]["card_name"] = ""
    f.write_text(json.dumps(cards))
    jid = post(client, "swayam", **SW)
    job = wait_done(client, jid, timeout=90)
    assert job["status"] == "partially_completed" and job["stats"]["items"] == 3 and job["stats"]["invalid"] == 1
    e = errors(client, jid)[-1]
    assert (e["error_code"], e["stage"], e["retryable"]) == ("VALIDATION_FAILED", "VALIDATE", False)


def test_swayam_explorer_timeout(client, swayam):
    swayam.mode = "no_cards"
    jid = post(client, "swayam", max_clicks=1, workers=1)      # real crawl against a page with no cards
    job = wait_done(client, jid, timeout=90)
    e = errors(client, jid)[-1]
    assert job["status"] == "failed"
    assert (e["error_code"], e["stage"], e["job_continues"]) == ("NETWORK_TIMEOUT", "FETCH_LIST", False)


def test_swayam_crawl(client, swayam):
    jid = post(client, "swayam", max_clicks=1, workers=2, fresh=True, limit=2)
    job = wait_done(client, jid, timeout=120)
    assert job["status"] == "completed" and job["stats"]["items"] == 2
    assert job["stats"]["duplicates"] >= 0


def test_swayam_parse_failure_in_process(swayam, monkeypatch):
    """to_row blows up for one course -> PARSE_ERROR at PARSE, the rest still saved."""
    from app.scrapers.swayam import core
    from app.scrapers.swayam.scraper import SwayamScraper
    real = core.to_row

    def flaky(card, rec):
        if card.url.endswith("/c/2/preview"):
            raise KeyError("summary")
        return real(card, rec)

    monkeypatch.setattr(core, "to_row", flaky)
    out = io.StringIO()
    sc = SwayamScraper("e" * 32, {"reuse_cards": True, "fresh": True, "workers": 2}, out=out)
    assert sc.execute() == 0
    evs = [json.loads(l) for l in out.getvalue().splitlines()]
    err = [e for e in evs if e["event_type"] == "error"]
    assert [(e["error_code"], e["stage"], e["error_type"]) for e in err] == [("PARSE_ERROR", "PARSE", "KeyError")]
    assert err[0]["traceback"] and sc.saved == 3
    assert evs[-1]["event_type"] == "done" and evs[-1]["stage"] == "PARTIALLY_COMPLETED"


def test_swayam_progress_is_real(client, swayam):
    swayam.slow = 0.3
    jid = post(client, "swayam", **SW)
    with client.stream("GET", f"/jobs/{jid}/events") as r:
        evs = [json.loads(l[5:]) for l in r.iter_lines() if l.startswith("data:") and l != "data: {}"]
    detail = [e for e in evs if e["stage"] == "FETCH_DETAIL" and e["event_type"] == "progress"]
    assert detail and all(15 <= e["percent"] <= 80 for e in detail)
    assert detail[-1]["items_done"] == 4 == detail[-1]["items_total"] and detail[-1]["percent"] == 80
    assert detail[-1]["message"] == "Scraped 4/4 courses"
    pct = [e["percent"] for e in evs if e["event_type"] in ("progress", "stage")]
    assert pct == sorted(pct)                                  # monotonic
    stages = [e["stage"] for e in evs if e["event_type"] == "stage"]
    assert stages[:4] == ["INIT", "FETCH_LIST", "PARSE", "FETCH_DETAIL"]


def test_swayam_cancel_no_orphan_chromium(client, swayam):
    swayam.slow = 2.5
    before = playwright_pids()
    jid = post(client, "swayam", **SW)
    deadline = time.time() + 30
    while time.time() < deadline and not (playwright_pids() - before):
        time.sleep(0.3)
    assert playwright_pids() - before, "Chromium never started"
    assert client.post(f"/jobs/{jid}/cancel").status_code == 200
    assert client.get(f"/jobs/{jid}").json()["status"] in ("cancelling", "cancelled")
    job = wait_done(client, jid, timeout=40)
    assert job["status"] == "cancelled"
    deadline = time.time() + 10
    while time.time() < deadline and playwright_pids() - before:
        time.sleep(0.3)
    assert not (playwright_pids() - before), "orphan Chromium left behind"
    assert "Traceback" not in job["message"]


def test_source_concurrency_limits(client, swayam):
    swayam.slow = 3
    a, b = post(client, "swayam", **SW), post(client, "swayam", **SW)
    d = post(client, "demo", items=500, delay_ms=100)
    time.sleep(1.5)
    st = {j: client.get(f"/jobs/{j}").json()["status"] for j in (a, b, d)}
    assert st[a] == "running" and st[b] == "queued" and st[d] == "running"   # swayam max 1, global 2
    for j in (b, d, a):
        client.post(f"/jobs/{j}/cancel")
    for j in (a, b, d):
        wait_done(client, j, timeout=40)


# ----------------------------------------------------------------------------- platform
def test_write_failure(client):
    jid = post(client, "demo", items=5, delay_ms=0, fail_save=True)
    job = wait_done(client, jid)
    e = errors(client, jid)[-1]
    assert job["status"] == "failed" and not job["has_output"]
    assert (e["error_code"], e["stage"]) == ("SAVE_ERROR", "SAVE")
    from app import config
    assert len((config.job_dir(jid) / "partial" / "items.jsonl").read_text().splitlines()) == 5   # partial kept
    assert_friendly(client.get(f"/jobs/{jid}").json())


def test_subprocess_crash_has_real_reason(client):
    jid = post(client, "demo", items=10, delay_ms=0, crash_at=3)
    job = wait_done(client, jid)
    e = errors(client, jid)[-1]
    assert job["status"] == "failed"
    assert e["error_code"] == "PROCESS_EXITED_UNEXPECTEDLY" and e["retryable"] and not e["job_continues"]
    assert "code 7" in e["technical_message"] and "simulated hard crash" in e["traceback"]
    assert "exit code 1" not in job["message"]
    assert (client.post(f"/jobs/{jid}/rerun").status_code == 201)


def test_uncaught_exception_is_structured(client):
    jid = post(client, "demo", items=5, delay_ms=0, fail_at=2)
    e = errors(client, post_wait(client, jid))[-1]
    assert (e["error_code"], e["error_type"], e["stage"]) == ("SCRAPER_ERROR", "RuntimeError", "FETCH_DETAIL")
    assert e["technical_message"] == "RuntimeError: demo crash at item 2" and e["file"] == "scraper.py"


def post_wait(client, jid):
    wait_done(client, jid)
    return jid


def test_worker_never_touches_sqlite():
    code = ("import sys; import app.core.worker; from app.scrapers.registry import get_scraper_class, REGISTRY\n"
            "[get_scraper_class(s) for s in REGISTRY]\n"
            "print('app.db.session' in sys.modules or 'sqlalchemy' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.dirname(__file__)))
    assert out.stdout.strip().endswith("False"), out.stderr


def test_no_job_left_running(client):
    from app.core.job_manager import manager
    manager.reconcile()
    assert not [j for j in client.get("/jobs?limit=200").json() if j["status"] in ("running", "cancelling")]
