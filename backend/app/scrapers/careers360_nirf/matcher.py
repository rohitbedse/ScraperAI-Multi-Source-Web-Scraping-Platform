"""College lookup on Careers360 and identity matching against NIRF seed entries.

The site's own /search/ is disallowed by robots.txt, so candidates come from the public
profile sitemap. Names are normalized, scored by similarity, and a profile is accepted only
when it is clearly the best match; otherwise the candidates are recorded and nothing is guessed.
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from app.scrapers.careers360_nirf import config
from app.scrapers.careers360_nirf.http_client import PoliteClient
from app.scrapers.careers360_nirf.schemas import Candidate, MatchInfo, Seed

logger = logging.getLogger("scraper.careers360_nirf.matcher")

_STOP = {"of", "the", "and", "in", "for", "a"}
_SUFFIX_PENALTY = 0.05
_PROFILE = re.compile(r"^https://www\.careers360\.com/(university|colleges)/([a-z0-9\-]+)$")


def normalize_name(text: str) -> str:
    """Lower-case, '&'->'and', fold 'S.R.M.' -> 'srm', drop punctuation, collapse spaces."""
    t = text.lower().replace("&", " and ")
    t = re.sub(r"\b(?:[a-z]\.){2,}", lambda m: m.group(0).replace(".", ""), t)
    t = re.sub(r"[`'’\"]", "", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _tokens(norm: str) -> list[str]:
    return norm.split()


def _sorted_tokens(s: str) -> str:
    return " ".join(sorted(t for t in s.split() if t not in _STOP))


def similarity(a: str, b: str) -> float:
    """Character similarity, also word-order-insensitive ('Kerala University' ~ 'University of Kerala')."""
    return max(SequenceMatcher(None, a, b).ratio(),
               SequenceMatcher(None, _sorted_tokens(a), _sorted_tokens(b)).ratio())


def name_variants(name: str) -> list[str]:
    """Normalized seed name plus a version without '(...)' remarks and 'Deemed-to-be-university'."""
    stripped = re.sub(r"\([^)]*\)", " ", name)
    stripped = re.sub(r"\bdeemed[\s\-]*to[\s\-]*be[\s\-]*university\b", " ", stripped, flags=re.I)
    out: list[str] = []
    for v in (name, stripped):
        n = normalize_name(v)
        if n and n not in out:
            out.append(n)
    return out


def slug_of(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


class SitemapIndex:
    """Profile URLs from the sitemap with an inverted token index for candidate lookup."""

    def __init__(self, urls: list[str]):
        self.urls: list[str] = []
        self._norm: list[str] = []
        self._inv: dict[str, set[int]] = defaultdict(set)
        for u in urls:
            if not _PROFILE.match(u):
                continue
            i = len(self.urls)
            norm = normalize_name(slug_of(u).replace("-", " "))
            self.urls.append(u)
            self._norm.append(norm)
            for tok in set(_tokens(norm)) - _STOP:
                self._inv[tok].add(i)

    def __len__(self) -> int:
        return len(self.urls)

    def candidates(self, seed: Seed, limit: int = 60) -> list[Candidate]:
        names = name_variants(seed.name)
        city = normalize_name(seed.city)
        want = {t for n in names for t in _tokens(n)} - _STOP
        hits: dict[int, int] = defaultdict(int)
        for tok in want:
            for i in self._inv.get(tok, ()):
                hits[i] += 1
        pool = sorted(hits, key=lambda i: (-hits[i], len(self._norm[i])))[:limit]
        scored = []
        for i in pool:
            slug = self._norm[i]
            variants = [(slug, 0.0)]
            if city and slug.endswith(" " + city):       # "madras medical college chennai" -> without city
                variants.append((slug[: -len(city) - 1], 0.0))
            toks = slug.split()
            for k in (1, 2):                              # a different, unknown trailing city: small penalty
                if len(toks) > k + 1:
                    variants.append((" ".join(toks[:-k]), _SUFFIX_PENALTY))
            best = max(max(max(similarity(n, v) - pen for v, pen in variants), similarity(f"{n} {city}", slug))
                       for n in names)
            scored.append(Candidate(url=self.urls[i], score=round(best, 4)))
        scored.sort(key=lambda c: (-c.score, c.url))
        return scored


def parse_sitemap(xml_text: str) -> list[str]:
    return re.findall(r"<loc>\s*(.*?)\s*</loc>", xml_text)


def load_sitemap_index(client: PoliteClient, cache_file: Path = config.SITEMAP_CACHE_FILE,
                       ttl_days: float = config.SITEMAP_TTL_DAYS) -> SitemapIndex:
    """Use the cached sitemap when fresh, otherwise download it once."""
    cache_file = Path(cache_file)
    if cache_file.is_file() and (time.time() - cache_file.stat().st_mtime) < ttl_days * 86400:
        logger.info("using cached sitemap %s", cache_file)
        xml = cache_file.read_text(encoding="utf-8")
    else:
        logger.info("downloading profile sitemap %s", config.SITEMAP_URL)
        xml = client.get(config.SITEMAP_URL, timeout=config.SITEMAP_TIMEOUT_SECONDS)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(xml, encoding="utf-8")
        tmp.replace(cache_file)
    return SitemapIndex(parse_sitemap(xml))


def _slug_has_city(url: str, city: str) -> bool:
    c = normalize_name(city)
    return bool(c) and normalize_name(slug_of(url).replace("-", " ")).endswith(" " + c)


def match_seed(seed: Seed, index: SitemapIndex) -> MatchInfo:
    """Name-based decision. State/city are verified later against the profile page itself."""
    cands = index.candidates(seed)
    shown = cands[: config.CANDIDATES_RECORDED]
    if not cands or cands[0].score < config.MATCH_THRESHOLD:
        near = [c for c in shown if c.score >= 0.6]
        return MatchInfo(status="unmatched", candidates=near, score=cands[0].score if cands else None,
                         reason=f"no candidate reached similarity {config.MATCH_THRESHOLD}")
    top = cands[0]
    close = [c for c in cands if c.score >= config.MATCH_THRESHOLD and top.score - c.score <= config.MATCH_MARGIN]
    if len(close) > 1:
        by_city = [c for c in close if _slug_has_city(c.url, seed.city)]
        if len(by_city) == 1:
            return MatchInfo(status="matched", url=by_city[0].url, score=by_city[0].score,
                             reason="several close names; city in profile URL broke the tie", candidates=shown)
        return MatchInfo(status="ambiguous", score=top.score, candidates=close[: config.CANDIDATES_RECORDED],
                         reason=f"{len(close)} candidates within {config.MATCH_MARGIN} of the best score")
    return MatchInfo(status="matched", url=top.url, score=top.score, candidates=shown, reason="single best candidate")


def verify_location(seed: Seed, page_city: Optional[str], page_state: Optional[str]) -> tuple[Optional[bool], Optional[bool]]:
    """(state_verified, city_match); None where the page gives no value to compare."""
    state_ok = None if not page_state else normalize_name(page_state) == normalize_name(seed.state)
    city_ok = None if not page_city else normalize_name(page_city) == normalize_name(seed.city)
    return state_ok, city_ok
