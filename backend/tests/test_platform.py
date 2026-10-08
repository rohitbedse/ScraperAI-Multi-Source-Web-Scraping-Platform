import json
import os
import time

import pytest

from tests.conftest import wait_done


def make(client, **params):
    r = client.post("/jobs", json={"source": "demo", "params": params})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_imports_and_registry():
    from app.scrapers.base import BaseScraper
    from app.scrapers.registry import REGISTRY, get_scraper_class
    assert {"swayam", "mindler"} <= set(REGISTRY)
    for sid in REGISTRY:
        assert issubclass(get_scraper_class(sid), BaseScraper)


def test_health_and_sources(client):
    assert client.get("/health").json() == {"status": "ok"}
    src = {s["id"]: s for s in client.get("/sources").json()}
    assert src["swayam"]["status"] == "available" and src["mindler"]["name"]
    assert set(src["swayam"]) >= {"id", "name", "description", "status"}


def test_validation(client):
    assert client.post("/jobs", json={"source": "nope"}).status_code == 404
    assert client.post("/jobs", json={"source": "../etc"}).status_code == 422
    assert client.post("/jobs", json={"source": "demo", "params": {"items": 0}}).status_code == 422
    assert client.post("/jobs", json={"source": "demo", "params": {"cmd": "evil"}}).status_code == 422
    assert client.get("/jobs/..%2f..%2fsecret").status_code in (404, 422)
    assert client.get("/jobs/" + "0" * 32).status_code == 404
    assert client.get("/jobs/" + "0" * 32 + "/download").status_code == 404


def test_run_persist_sse_download(client):
    jid = make(client, items=6, delay_ms=20, soft_errors=1)
    with client.stream("GET", f"/jobs/{jid}/events") as r:
        lines = list(r.iter_lines())
    events = [json.loads(l[5:]) for l in lines if l.startswith("data:") and l != "data: {}"]
    types = [e["event_type"] for e in events]
    assert "stage" in types and "progress" in types and "error" in types and "done" in types
    assert "event: end" in lines[-3:]
    job = wait_done(client, jid)
    assert job["status"] == "partially_completed" and job["items_done"] == 6 and job["error_count"] == 1
    assert job["errors"][0]["error_code"] == "NETWORK_TIMEOUT" and job["errors"][0]["traceback"] is None
    # persisted: replay after a "refresh", and resume from Last-Event-ID
    with client.stream("GET", f"/jobs/{jid}/events", headers={"Last-Event-ID": "3"}) as r:
        ids = [int(l[4:]) for l in r.iter_lines() if l.startswith("id:")]
    assert ids and min(ids) > 3
    out = client.get(f"/jobs/{jid}/download")
    assert out.status_code == 200 and len(out.json()["items"]) == 6
    assert client.get(f"/jobs/{jid}").json()["stats"]["items"] == 6


def test_failure_keeps_partial(client):
    # items are only saved at DEDUPE now, so a crash during FETCH_DETAIL leaves raw data but no output
    jid = make(client, items=10, delay_ms=10, fail_at=4)
    job = wait_done(client, jid)
    assert job["status"] == "failed" and not job["has_output"]
    det = client.get(f"/jobs/{jid}?include_traceback=true").json()
    assert "demo crash" in det["errors"][0]["traceback"] and det["errors"][0]["job_continues"] is False


def test_failure_without_items(client):
    jid = make(client, items=5, delay_ms=0, fail_at=1)
    job = wait_done(client, jid)
    assert job["status"] == "failed" and not job["has_output"]
    assert client.get(f"/jobs/{jid}/download").status_code == 404


def test_cancel_and_rerun(client):
    jid = make(client, items=500, delay_ms=100)
    time.sleep(1.5)
    assert client.post(f"/jobs/{jid}/cancel").status_code == 200
    job = wait_done(client, jid)
    assert job["status"] == "cancelled"
    assert client.post(f"/jobs/{jid}/cancel").status_code == 409
    new = client.post(f"/jobs/{jid}/rerun").json()
    assert new["id"] != jid and new["params"]["items"] == 500
    client.post(f"/jobs/{new['id']}/cancel")
    wait_done(client, new["id"])


def test_concurrency_limit(client):
    ids = [make(client, items=500, delay_ms=100) for _ in range(3)]
    time.sleep(1)
    statuses = [client.get(f"/jobs/{i}").json()["status"] for i in ids]
    assert statuses.count("running") == 2 and statuses.count("queued") == 1
    for i in reversed(ids):
        client.post(f"/jobs/{i}/cancel")
    for i in ids:
        wait_done(client, i)
    assert len(client.get("/jobs?limit=5").json()) == 5


def test_api_key(client, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "API_KEY", "secret")
    assert client.get("/health").status_code == 200
    assert client.get("/sources").status_code == 401
    assert client.get("/sources", headers={"X-API-Key": "secret"}).status_code == 200
    assert client.get("/sources?api_key=secret").status_code == 200


def test_cleanup_ttl(client):
    from app import config
    from app.core.job_manager import manager
    jid = make(client, items=2, delay_ms=0)
    wait_done(client, jid)
    out = config.job_dir(jid) / "output" / "result.json"
    old = time.time() - 30 * 86400
    for f in config.job_dir(jid).rglob("*"):
        os.utime(f, (old, old))
    manager.cleanup_outputs()
    assert not out.exists() and client.get(f"/jobs/{jid}/download").status_code == 404


# ------------------------------------------------------------- parser tests (no network)
def test_mindler_parser():
    from app.scrapers.mindler import core
    cd = [{"id": 1, "career_id": "c1", "career_name": "Pilot",
           "career_entrance": [{"entrance_exam": "NDA", "key_elements": "<p>Math</p>"}],
           "pros_cons": [{"pros": "<li>Pay</li>", "cons": "<li>Hours</li>"}],
           "career_opportunities": [{"Name": "<b>Airline</b>", "Description": "x"}]},
          {"id": 2, "career_id": "c1", "career_name": "Pilot dup"}]
    parsed = core.parse_career_details(cd)
    assert parsed[0]["entrance_exams"][0]["key_elements"] == "Math"
    assert parsed[0]["pros_cons"]["Pros"] == ["Pay"]
    assert len(core.deduplicate_subcareers(parsed)) == 1
    assert core.subject_id_for(" Fine Arts ") == "fine-arts"


def test_swayam_row_and_validation():
    from app.scrapers.swayam import core
    card = core.ExplorerCard(url="https://onlinecourses.swayam2.ac.in/x/preview", card_name="Intro to R",
                             source="NPTEL", card_duration="12 Weeks", explorer_tab="Upcoming")
    rec = {"course_name": "Intro to R", "enrolled": "1,234", "instructor": "Prof X",
           "info": "Intended audience: UG students\nPre-requisites: none",
           "summary": {"Start Date": "01 Jan 2026", "Course Level": "Under Graduate", "Credit Points": "3",
                       "Exam Date": "32 Foo 2026"}}
    good, bad = core.validate([core.to_row(card, rec)])
    assert len(good) == 1 and good[0].no_of_enrolled_users == 1234 and str(good[0].start_date) == "2026-01-01"
    assert good[0].academic_level == "UG" and [b["field"] for b in bad] == ["exam_date"]


# ------------------------------------------------------------- live (opt-in: RUN_LIVE=1)
live = pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="set RUN_LIVE=1 for network tests")


@live
@pytest.mark.parametrize("source,params", [("mindler", {"limit": 2}),
                                           ("swayam", {"limit": 3, "workers": 2, "max_clicks": 1})])
def test_live_scrapers(client, source, params):
    jid = client.post("/jobs", json={"source": source, "params": params}).json()["id"]
    job = wait_done(client, jid, timeout=300)
    assert job["status"] in ("completed", "partially_completed"), job
    assert client.get(f"/jobs/{jid}/download").json()["total_items"] >= 1
