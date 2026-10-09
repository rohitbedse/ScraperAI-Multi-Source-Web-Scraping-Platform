"""Polite HTTP client: robots.txt, host allow-list, rate limit, timeouts, retries with backoff.

No stealth: an honest User-Agent, no proxies, no CAPTCHA handling. A 403/429/CAPTCHA raises
BlockedError and the caller stops that college.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Callable, Optional
from urllib.parse import urlsplit

import requests

from app.scrapers.careers360_nirf import config

logger = logging.getLogger("scraper.careers360_nirf.http")


class BlockedError(Exception):
    """The site refused us (403/429/CAPTCHA). Do not retry or work around it."""


class FetchError(Exception):
    """Request failed after all retries."""


class HttpStatusError(Exception):
    def __init__(self, url: str, status: int):
        super().__init__(f"HTTP {status} for {url}")
        self.url, self.status = url, status


class RobotsDisallowed(Exception):
    """robots.txt (or the host allow-list) forbids this URL; it was not requested."""


class RobotsPolicy:
    """Google-style robots.txt matching (`*` wildcard, `$` anchor, longest rule wins) for `User-agent: *`."""

    def __init__(self, text: str):
        self._rules: list[tuple[bool, re.Pattern, int]] = []
        agents: list[str] = []
        in_rules = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            field, value = (p.strip() for p in line.split(":", 1))
            field = field.lower()
            if field == "user-agent":
                if in_rules:
                    agents, in_rules = [], False
                agents.append(value.lower())
            elif field in ("allow", "disallow"):
                in_rules = True
                if "*" in agents and value:
                    self._rules.append((field == "allow", self._compile(value), len(value)))

    @staticmethod
    def _compile(pattern: str) -> re.Pattern:
        anchored = pattern.endswith("$")
        body = re.escape(pattern.rstrip("$")).replace(r"\*", ".*")
        return re.compile("^" + body + ("$" if anchored else ""))

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        best_len, verdict = -1, True
        for allow, rx, length in self._rules:
            if rx.match(target) and (length > best_len or (length == best_len and allow)):
                best_len, verdict = length, allow
        return verdict


class PoliteClient:
    def __init__(self, delay: float = config.REQUEST_DELAY_SECONDS,
                 timeout: float = config.REQUEST_TIMEOUT_SECONDS,
                 max_retries: int = config.MAX_RETRIES,
                 session: Optional[requests.Session] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 robots_text: Optional[str] = None):
        self.delay, self.timeout, self.max_retries = delay, timeout, max_retries
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT, "Accept-Language": "en"})
        self._sleep, self._clock = sleep, clock
        self._last_request = -1e9
        self._robots: Optional[RobotsPolicy] = RobotsPolicy(robots_text) if robots_text is not None else None
        self.requests_made = 0
        self.blocked_count = 0

    # ------------------------------------------------------------------ robots
    def _ensure_robots(self) -> RobotsPolicy:
        if self._robots is None:
            try:
                text = self._request(config.ROBOTS_URL, check_robots=False)
            except HttpStatusError as exc:
                if exc.status == 404:           # no robots.txt = everything allowed
                    text = ""
                else:
                    raise RobotsDisallowed(f"cannot read robots.txt ({exc}); refusing to crawl") from exc
            except (FetchError, BlockedError) as exc:
                raise RobotsDisallowed(f"cannot read robots.txt ({exc}); refusing to crawl") from exc
            self._robots = RobotsPolicy(text)
        return self._robots

    def check_allowed(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        if host not in config.ALLOWED_HOSTS:
            raise RobotsDisallowed(f"host {host!r} is not in the allow-list: {url}")
        if not self._ensure_robots().allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

    # ------------------------------------------------------------------ fetching
    def get(self, url: str, timeout: Optional[float] = None) -> str:
        """Fetch a page as text. Raises RobotsDisallowed, BlockedError, HttpStatusError or FetchError."""
        self.check_allowed(url)
        return self._request(url, timeout=timeout)

    def _throttle(self) -> None:
        wait = self.delay - (self._clock() - self._last_request)
        if wait > 0:
            self._sleep(wait)

    def _request(self, url: str, check_robots: bool = True, timeout: Optional[float] = None) -> str:
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                backoff = config.BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                logger.warning("retry %d/%d for %s in %.1fs (%s)", attempt, self.max_retries, url, backoff, last_exc)
                self._sleep(backoff)
            self._throttle()
            self._last_request = self._clock()
            self.requests_made += 1
            try:
                resp = self.session.get(url, timeout=timeout or self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                continue
            status = resp.status_code
            if status in config.BLOCK_STATUSES:
                self.blocked_count += 1
                logger.error("BLOCKED: HTTP %s from %s (Retry-After=%s). Stopping, not working around it.",
                             status, url, resp.headers.get("Retry-After"))
                raise BlockedError(f"HTTP {status} for {url}")
            if status >= 500 or status == 408:
                last_exc = HttpStatusError(url, status)
                continue
            if status >= 400:
                raise HttpStatusError(url, status)
            text = resp.text
            head = text[:6000].lower()
            if "window.INITIAL_STATE" not in text and any(m in head for m in config.CAPTCHA_MARKERS):
                self.blocked_count += 1
                logger.error("BLOCKED: challenge page returned for %s", url)
                raise BlockedError(f"challenge/CAPTCHA page for {url}")
            return text
        raise FetchError(f"{url}: failed after {self.max_retries + 1} attempts ({last_exc})")
