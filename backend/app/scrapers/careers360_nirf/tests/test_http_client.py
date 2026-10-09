import pytest
import requests

from app.scrapers.careers360_nirf import config
from app.scrapers.careers360_nirf.http_client import (
    BlockedError, FetchError, HttpStatusError, PoliteClient, RobotsDisallowed, RobotsPolicy)

ROBOTS = """
User-agent: *
Disallow: /search/
Disallow: */api/*
Disallow: *?sort*
Disallow: /*.pdf$
Allow: /search/public

User-agent: Mediapartners-Google
User-agent: *
Disallow: /user/*
"""
OK = "<html>window.INITIAL_STATE={}</html>"


class Resp:
    def __init__(self, status=200, text=OK, headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}


class Session:
    def __init__(self, *responses):
        self.responses, self.calls, self.headers = list(responses), [], {}

    def get(self, url, timeout=None):
        self.calls.append(url)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


def client(*responses, **kw):
    sleeps = []
    c = PoliteClient(delay=kw.pop("delay", 0), max_retries=kw.pop("max_retries", 3),
                     session=Session(*responses), sleep=sleeps.append, robots_text=kw.pop("robots", ""), **kw)
    c.sleeps = sleeps
    return c


URL = "https://www.careers360.com/university/x"


def test_robots_policy_wildcards_and_allow_override():
    p = RobotsPolicy(ROBOTS)
    assert not p.allowed("https://www.careers360.com/search/x")
    assert p.allowed("https://www.careers360.com/search/public")            # longer Allow wins
    assert not p.allowed("https://www.careers360.com/a/api/b")
    assert not p.allowed("https://www.careers360.com/x?sort=1")
    assert not p.allowed("https://www.careers360.com/file.pdf")
    assert p.allowed("https://www.careers360.com/file.pdf.html")            # `$` anchors
    assert not p.allowed("https://www.careers360.com/user/me")               # second group also targets *
    assert p.allowed("https://www.careers360.com/university/x/courses?page=2")


def test_disallowed_urls_and_foreign_hosts_are_never_requested():
    c = client(Resp(), robots=ROBOTS)
    with pytest.raises(RobotsDisallowed):
        c.get("https://www.careers360.com/search/q")
    with pytest.raises(RobotsDisallowed):
        c.get("https://evil.example.com/page")
    assert c.session.calls == []


def test_unreadable_robots_means_no_crawling():
    c = PoliteClient(delay=0, max_retries=0, session=Session(Resp(500, "err")), sleep=lambda s: None)
    with pytest.raises(RobotsDisallowed, match="robots.txt"):
        c.get(URL)


def test_retry_with_backoff_then_success():
    c = client(Resp(503), Resp(500), Resp())
    assert c.get(URL) == OK
    assert len(c.session.calls) == 3
    assert c.sleeps == [config.BACKOFF_BASE_SECONDS, config.BACKOFF_BASE_SECONDS * 2]


def test_network_errors_retried_then_fetch_error():
    c = client(requests.ConnectionError("down"), max_retries=2)
    with pytest.raises(FetchError, match="3 attempts"):
        c.get(URL)
    assert len(c.session.calls) == 3


@pytest.mark.parametrize("status", [403, 429])
def test_block_statuses_stop_immediately_without_retry(status):
    c = client(Resp(status, "denied"))
    with pytest.raises(BlockedError):
        c.get(URL)
    assert len(c.session.calls) == 1 and c.blocked_count == 1 and c.sleeps == []


def test_captcha_page_is_a_block_not_data():
    c = client(Resp(200, "<html>Please solve this CAPTCHA to continue</html>"))
    with pytest.raises(BlockedError, match="CAPTCHA"):
        c.get(URL)


def test_page_with_state_is_not_mistaken_for_captcha():
    c = client(Resp(200, "<script>captcha</script>" + OK))
    assert "INITIAL_STATE" in c.get(URL)


def test_404_is_not_retried():
    c = client(Resp(404, "nope"))
    with pytest.raises(HttpStatusError) as ei:
        c.get(URL)
    assert ei.value.status == 404 and len(c.session.calls) == 1


def test_rate_limit_delay_between_requests():
    t = [0.0]
    sleeps = []
    c = PoliteClient(delay=2.0, session=Session(Resp()), robots_text="", sleep=sleeps.append, clock=lambda: t[0])
    c.get(URL)                       # first call: no wait
    t[0] = 0.5
    c.get(URL)                       # only 0.5s elapsed -> waits 1.5s
    assert sleeps == [pytest.approx(1.5)]
    assert c.requests_made == 2
