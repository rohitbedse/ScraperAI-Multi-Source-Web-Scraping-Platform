"""NIRF source: discovery, parsing, validation, outputs, incremental merge, offline re-parse and errors.
All HTTP goes to a local fake nirfindia.org (same HTML shape as the real 2025 pages); nothing touches the network."""
import csv
import io
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from bs4 import BeautifulSoup

from app.scrapers.nirf import core
from app.scrapers.nirf import scraper as nirf
from app.scrapers.registry import REGISTRY, get_scraper_class
from tests.conftest import wait_done

YEAR = 2025


def _row(iid, name, city, state, score, rank, params=None, cat="Engineering"):
    params = params or {"TLR": 90.5, "RPC": 80.0}
    nested = ("<table class='table'><thead><tr>" + "".join(f"<th>{k} (100)</th>" for k in params) +
              "</tr></thead><tbody><tr>" + "".join(f"<td>{v}</td>" for v in params.values()) +
              "</tr></tbody></table>")
    pdf = f"/nirfpdfcdn/{YEAR}/pdf/{cat}/{iid}.pdf"
    return (f"<tr><td>{iid}</td><td>{name}<div style='float:right;'>"
            f"<a class='btn-more-details' href='javascript:;'><span>More Details</span></a>"
            f"<a style='display:none' class='close_data'>Close</a> | <a href='{pdf}'><img></a> | "
            f"<a href='/nirfpdfcdn/{YEAR}/graph/{cat}/{iid}.jpg'><img></a></div>"
            f"<div class='tbl_hidden' style='display:none'>{nested}</div></td>"
            f"<td>{city}</td><td>{state}</td><td>{score}</td><td>{rank}</td></tr>")


def _page(rows, links=""):
    return (f"<html><body>{links}<table id='tbl_overall'><thead><tr><th>Institute ID</th><th>Name</th>"
            f"<th>City</th><th>State</th><th>Score</th><th>Rank</th></tr></thead><tbody>{''.join(rows)}"
            f"</tbody></table></body></html>")


def _all_page(rows):
    body = "".join(f"<tr><td>{n}</td><td>{c}</td><td>{s}</td></tr>" for n, c, s in rows)
    return (f"<html><body><table><thead><tr><th>Name</th><th>City</th><th>State</th></tr></thead>"
            f"<tbody>{body}</tbody></table></body></html>")


def site(eng_score=88.1):
    eng_links = ("<a href='EngineeringRanking150.html'>101-150</a>"
                 "<a href='EngineeringRankingALL.html'>Participating Institutions</a>")
    return {
        f"/Rankings/{YEAR}/Ranking.html": "<html><body><a href='EngineeringRanking.html'>Engineering</a>"
                                          "<a href='/Rankings/2025/PharmacyRanking.html'>Pharmacy</a>"
                                          "<a href='/Rankings/2024/OverallRanking.html'>old</a></body></html>",
        f"/Rankings/{YEAR}/EngineeringRanking.html": _page(
            [_row("IR-E-U-0456", "Indian Institute of Technology Madras", "Chennai", "Tamil Nadu", eng_score, 1),
             _row("IR-E-I-1074", "Indian Institute of Technology Delhi", "New Delhi", "Delhi", 86.0, 2)], eng_links),
        f"/Rankings/{YEAR}/EngineeringRanking150.html": _page(
            [_row("IR-E-C-1", "Band College", "Pune", "Maharashtra", "", "101-150")]),
        f"/Rankings/{YEAR}/EngineeringRankingALL.html": _all_page(
            [("Government Degree College", "Anantnag", "J&K"), ("Government Degree College", "Anantnag", "J&K"),
             ("Other College", "Agra", "UP")]),
        f"/Rankings/{YEAR}/PharmacyRanking.html": _page(
            [_row("IR-P-U-0107", "Jamia Hamdard", "New Delhi", "Delhi", 80.0, 1,
                  {"TLR": 80.51, "PERCEPTION": 100.0}, "Pharmacy")]),
    }


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        o = self.server.owner
        o.hits.append(self.path)
        status = o.status.get(self.path, 200 if self.path in o.pages else 404)
        body = (o.pages.get(self.path, "") if status == 200 else "nope").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeNirf:
    def __init__(self):
        self.pages, self.status, self.hits = site(), {}, []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.srv.owner, self.srv.daemon_threads = self, True
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


@pytest.fixture
def fake():
    f = FakeNirf()
    yield f
    f.srv.shutdown()


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(nirf, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setenv("NIRF_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path / "state"


def run(fake, params=None):
    out = io.StringIO()
    sc = nirf.NirfScraper(uuid.uuid4().hex, {"year": YEAR, **(params or {})}, out=out)
    real = sc.make_fetcher

    def fast():
        f = real()
        f.base, f.delay, f.sleep = fake.base, 0, (lambda s: None)
        return f

    sc.make_fetcher = fast
    rc = sc.execute()
    evs = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
    items = json.load(open(sc.output_path, encoding="utf-8"))["items"] if sc.output_path.exists() else []
    return sc, rc, evs, items


def errors(evs):
    return [e for e in evs if e["event_type"] == "error"]


# ------------------------------------------------------------------------------------------- registry / params
def test_registered_and_listed(client):
    assert REGISTRY["nirf"] == "app.scrapers.nirf.scraper:NirfScraper"
    assert get_scraper_class("nirf").max_concurrent == 1
    src = {s["id"]: s for s in client.get("/sources").json()}["nirf"]
    assert src["status"] == "available"
    assert set(src["params_schema"]["properties"]) == {"year", "categories", "no_bands", "no_all",
                                                       "incremental", "offline", "delay"}
    assert client.post("/jobs", json={"source": "nirf", "params": {"bogus": 1}}).status_code == 422


# ------------------------------------------------------------------------------------------- parsing layer
def test_parser_keeps_nested_table_out_and_reads_parameters():
    html = site()[f"/Rankings/{YEAR}/EngineeringRanking.html"]
    recs, warns = core.parse_ranking_page(html, url="http://x/Rankings/2025/EngineeringRanking.html", year=YEAR,
                                          category="Engineering", slug="Engineering", list_type="ranked",
                                          rank_band=None)
    assert not warns and len(recs) == 2                       # nested parameter rows did not leak in
    r = recs[0]
    assert r["institution_name"] == "Indian Institute of Technology Madras"
    assert (r["institution_id"], r["city"], r["rank"], r["score"]) == ("IR-E-U-0456", "Chennai", 1, 88.1)
    assert r["parameter_scores"] == {"TLR": {"score": 90.5, "max": 100.0}, "RPC": {"score": 80.0, "max": 100.0}}
    assert r["pdf_url"].endswith("/pdf/Engineering/IR-E-U-0456.pdf") and r["graph_url"].endswith(".jpg")
    rows = core._own_rows(BeautifulSoup(html, "lxml").find("table"))
    assert len(rows) == 3


def test_discovery_by_href_regex():
    cats = core.discover_categories(site()[f"/Rankings/{YEAR}/Ranking.html"], YEAR, "http://x")
    assert cats == {"Engineering": "http://x/Rankings/2025/EngineeringRanking.html",
                    "Pharmacy": "http://x/Rankings/2025/PharmacyRanking.html"}   # 2024 link ignored
    extra = core.discover_extra_pages(site()[f"/Rankings/{YEAR}/EngineeringRanking.html"],
                                      "http://x/Rankings/2025/EngineeringRanking.html", "Engineering")
    assert sorted(core.band_label(s, t, "Engineering") for s, t in extra.values()) == [
        ("participating", None), ("rank_band", "101-150")]
    assert core.select_categories(cats, ["pharmacy"]) == {"Pharmacy": cats["Pharmacy"]}


def test_validation_reports_without_failing():
    base = dict(ranking_year=YEAR, category="X", list_type="ranked", rank_band=None, city="c",
                institution_name="n", parameter_scores={})
    recs = [dict(base, institution_id="a", rank=2, score=5.0),
            dict(base, institution_id="b", rank=1, score=None, parameter_scores={"TLR": {"score": 120, "max": 100}}),
            dict(base, institution_id="b", rank=3, score=1.0)]
    issues = core.validate(recs)
    assert any("duplicate key" in i for i in issues)
    assert "X: ranks not monotonic" in issues
    assert "X: 1 ranked rows missing rank/score" in issues
    assert "X/b: TLR out of range" in issues


# ------------------------------------------------------------------------------------------- full job
def test_full_run_through_all_stages(fake, state):
    sc, rc, evs, items = run(fake)
    assert rc == 0 and not errors(evs)
    stages = [e["stage"] for e in evs if e["event_type"] == "stage"]
    assert stages == ["INIT", "FETCH_LIST", "FETCH_DETAIL", "PARSE", "VALIDATE", "DEDUPE", "SAVE"]
    by = {(r["category"], r["list_type"]) for r in items}
    assert by == {("Engineering", "ranked"), ("Engineering", "rank_band"), ("Engineering", "participating"),
                  ("Pharmacy", "ranked")}
    assert all(r["source"] == "NIRF" and r["source_url"].startswith(fake.base) for r in items)
    part = [r for r in items if r["list_type"] == "participating"]
    assert len(part) == 2                     # the identical row was deduped, the other college kept
    assert part[0]["institution_id"] is None and part[0]["rank"] is None   # missing fields are null
    band = next(r for r in items if r["list_type"] == "rank_band")
    assert band["rank_band"] == "101-150" and band["rank"] is None and band["score"] is None
    assert evs[-1]["stats"]["duplicates"] == 1 and evs[-1]["stats"]["pages"] == 4

    out = sc.output_path.parent
    rows = list(csv.DictReader(open(out / "nirf_2025.csv", encoding="utf-8-sig")))
    assert len(rows) == len(items) and {"TLR", "RPC", "PERCEPTION"} <= set(rows[0])
    manifest = json.load(open(out / "manifest.json", encoding="utf-8"))
    assert manifest["year"] == YEAR and len(manifest["pages"]) == 4 and manifest["errors"] == []
    assert (state / "nirf_2025.json").exists()           # complete run -> master dataset written


def test_category_filter_and_skips_leave_master_untouched(fake, state):
    sc, rc, evs, items = run(fake, {"categories": ["Engineering"], "no_bands": True, "no_all": True})
    assert rc == 0
    assert {(r["category"], r["list_type"]) for r in items} == {("Engineering", "ranked")}
    assert f"/Rankings/{YEAR}/EngineeringRanking150.html" not in fake.hits
    assert not (state / "nirf_2025.json").exists()


def test_unknown_category_fails_with_available_list(fake, state):
    sc, rc, evs, items = run(fake, {"categories": ["Nonsense"]})
    assert rc == 1
    assert "Available: Engineering, Pharmacy" in errors(evs)[-1]["message"]


def test_offline_reparse_uses_cache_only(fake, state):
    run(fake)
    fake.hits.clear()
    sc, rc, evs, items = run(fake, {"offline": True})
    assert rc == 0 and fake.hits == [] and len(items) == 6


def test_incremental_merges_and_counts_changes(fake, state):
    run(fake)
    fake.pages = site(eng_score=90.0)
    sc, rc, evs, items = run(fake, {"incremental": True, "categories": ["Engineering"]})
    assert rc == 0
    assert sc.meta["incremental"] == {"new": 0, "updated": 1, "total": 6}
    assert {r["category"] for r in items} == {"Engineering", "Pharmacy"}     # untouched category kept
    master = json.load(open(state / "nirf_2025.json", encoding="utf-8"))
    assert next(r for r in master if r["institution_id"] == "IR-E-U-0456")["score"] == 90.0


def test_failed_page_is_reported_and_job_continues(fake, state):
    fake.status[f"/Rankings/{YEAR}/EngineeringRankingALL.html"] = 403
    sc, rc, evs, items = run(fake)
    assert rc == 0 and evs[-1]["stage"] == "PARTIALLY_COMPLETED"
    err = errors(evs)[0]
    assert err["error_code"] == "BLOCKED_403" and err["job_continues"] and not err["retryable"]
    assert not any(r["list_type"] == "participating" for r in items)
    assert not (state / "nirf_2025.json").exists()           # incomplete run does not replace the master


def test_unreachable_index_fails_the_job(fake, state):
    fake.status[f"/Rankings/{YEAR}/Ranking.html"] = 500
    sc, rc, evs, items = run(fake)
    assert rc == 1 and errors(evs)[-1]["error_code"] == "HTTP_ERROR"


def test_layout_change_is_flagged(fake, state):
    fake.pages[f"/Rankings/{YEAR}/PharmacyRanking.html"] = "<html><table><tr><td>x</td></tr></table></html>"
    sc, rc, evs, items = run(fake)
    assert any(e["error_code"] == "LAYOUT_CHANGED" and "Pharmacy" in e["message"] for e in errors(evs))


def test_end_to_end_via_api(client, fake, monkeypatch, tmp_path):
    monkeypatch.setenv("NIRF_BASE_URL", fake.base)            # inherited by the job subprocess
    monkeypatch.setenv("NIRF_CACHE_DIR", str(tmp_path / "cache"))
    job = client.post("/jobs", json={"source": "nirf", "params": {"year": YEAR, "categories": ["Pharmacy"],
                                                                  "delay": 0.5}}).json()
    j = wait_done(client, job["id"])
    assert j["status"] == "completed", j
    data = client.get(f"/jobs/{job['id']}/download").json()
    assert [r["institution_name"] for r in data["items"]] == ["Jamia Hamdard"]
