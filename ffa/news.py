"""News scraping (RSS) and football-aware sentiment scoring."""
from __future__ import annotations

import html
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote_plus

import feedparser
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from . import http

GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

# Single-word valence tweaks for VADER (scale roughly -4..+4).
FOOTBALL_LEXICON = {
    "injured": -1.8, "injury": -1.4, "hamstring": -1.0, "ankle": -0.8, "knee": -0.9, "groin": -0.9,
    "concussion": -2.0, "questionable": -1.0, "doubtful": -2.2, "inactive": -2.0, "sidelined": -2.0,
    "benched": -2.2, "demoted": -2.2, "suspended": -2.5, "suspension": -2.5, "fumble": -1.5,
    "fumbled": -1.5, "fumbles": -1.5, "interception": -1.2, "interceptions": -1.2, "sacked": -1.0,
    "drops": -1.0, "dropped": -1.0, "touchdown": 1.5, "touchdowns": 1.5, "healthy": 1.5,
    "cleared": 1.6, "activated": 1.3, "breakout": 2.0, "dominant": 2.0, "explosive": 1.5,
    "workhorse": 1.8, "upside": 1.2, "sleeper": 0.8, "boom": 1.2, "bust": -1.5,
    # words VADER reads as negative but are neutral/positive in football
    "attack": 0.3, "aggressive": 0.8, "beast": 1.5, "kill": 0.0, "killer": 0.5, "hit": 0.0,
    "hits": 0.0, "fight": 0.0, "battle": 0.0, "blitz": 0.0, "crushed": 0.8,
}

# Multi-word phrases VADER can't see. Added directly to an article's [-1, 1] score.
PHRASES = [
    (r"ruled out|will not play|won't play|out for (the )?(season|year)|out indefinitely", -0.6),
    (r"injured reserve|placed on ir|season-ending|torn|fracture|surgery|carted off", -0.7),
    (r"concussion protocol", -0.5),
    (r"did not practice|didn't practice|\bdnp\b|missed practice", -0.4),
    (r"limited (practice|participant|in practice)", -0.2),
    (r"full(y)? (practice|participant|participated)|practiced in full|no injury designation", 0.4),
    (r"(expected|set|cleared|on track) to (play|return|start|suit up)|returns? to practice|off the injury report", 0.5),
    (r"lost (his |the )?(starting )?(job|role)|losing (snaps|work)|split backfield|committee|benched|demoted", -0.4),
    (r"career[- ]high|bell[- ]?cow|featured role|named (the )?starter|promoted|more snaps|target share|red[- ]zone (role|usage|work)", 0.3),
]

SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\.?$", re.I)


@dataclass
class Article:
    title: str
    summary: str
    source: str
    link: str
    published: datetime | None

    @property
    def text(self) -> str:
        return f"{self.title}. {self.summary}"


@dataclass
class Sentiment:
    score: float = 0.0           # -1 (very negative) .. +1 (very positive), shrunk toward 0
    n_articles: int = 0
    headlines: list[tuple[str, float, str]] = field(default_factory=list)  # (title, score, link)
    articles: list[dict] = field(default_factory=list)  # every scored article, newest first


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s or ""))).strip()


def _parse_feed(raw: bytes, default_source: str) -> list[Article]:
    feed = feedparser.parse(raw)
    out = []
    for e in feed.entries:
        pub = None
        if getattr(e, "published_parsed", None):
            pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        src = getattr(getattr(e, "source", None), "title", None) or default_source
        out.append(Article(_clean(e.get("title", "")), _clean(e.get("summary", "")), src, e.get("link", ""), pub))
    return out


def _name_parts(name: str) -> tuple[str, str]:
    base = SUFFIX.sub("", name).strip()
    return base, base.split()[-1] if base else name


class NewsScorer:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.vader = SentimentIntensityAnalyzer()
        self.vader.lexicon.update(FOOTBALL_LEXICON)
        self._generic: list[Article] | None = None

    # -- fetching ----------------------------------------------------------------
    def generic_articles(self) -> list[Article]:
        """General NFL feeds, fetched once per run and filtered per player by full name."""
        if self._generic is None:
            self._generic = []
            for url in self.cfg.get("generic_feeds", []):
                try:
                    self._generic += _parse_feed(http.get(url, ttl=3 * 3600), url.split("/")[2])
                except Exception as exc:  # one bad feed shouldn't kill the run
                    print(f"[warn] feed failed {url}: {exc}")
        return self._generic

    def player_articles(self, name: str) -> list[Article]:
        full, last = _name_parts(name)
        days = self.cfg.get("lookback_days", 7)
        url = GOOGLE_NEWS.format(q=quote_plus(f'"{full}" NFL when:{days}d'))
        try:
            arts = _parse_feed(http.get(url, ttl=3 * 3600), "Google News")
        except Exception as exc:
            print(f"[warn] news failed for {name}: {exc}")
            arts = []
        arts = [a for a in arts if last.lower() in a.text.lower()]
        arts += [a for a in self.generic_articles() if full.lower() in a.text.lower()]

        cutoff = time.time() - days * 86400
        seen, out = set(), []
        for a in arts:
            key = re.sub(r"\W+", "", a.title.lower())[:80]
            if key in seen or (a.published and a.published.timestamp() < cutoff):
                continue
            seen.add(key)
            out.append(a)
        out.sort(key=lambda a: a.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return out[: self.cfg.get("max_articles_per_player", 15)]

    # -- scoring -----------------------------------------------------------------
    def score_article(self, article: Article, name: str) -> float:
        _, last = _name_parts(name)
        sentences = re.split(r"(?<=[.!?])\s+", article.text)
        focus = " ".join(s for s in sentences if last.lower() in s.lower()) or article.text
        s = self.vader.polarity_scores(focus)["compound"]
        low = focus.lower()
        s += sum(w for pat, w in PHRASES if re.search(pat, low))
        return max(-1.0, min(1.0, s))

    def score(self, name: str, articles: list[Article]) -> Sentiment:
        if not articles:
            return Sentiment()
        half_life = self.cfg.get("half_life_days", 3)
        now = datetime.now(timezone.utc)
        num = den = 0.0
        scored, detail = [], []
        for a in articles:
            s = self.score_article(a, name)
            age = (now - a.published).total_seconds() / 86400 if a.published else half_life
            w = math.exp(-math.log(2) * max(age, 0) / half_life)
            num, den = num + w * s, den + w
            scored.append((a.title, round(s, 2), a.link))
            detail.append({"title": a.title, "score": round(s, 2), "source": a.source, "link": a.link,
                           "published": a.published.isoformat() if a.published else None})
        n = len(articles)
        k = self.cfg.get("shrinkage", 3)  # few articles -> pull toward neutral
        score = (num / den) * n / (n + k) if den else 0.0
        top = sorted(scored, key=lambda t: abs(t[1]), reverse=True)[:3]
        return Sentiment(round(score, 3), n, top, detail)

    def for_players(self, names: list[str]) -> dict[str, Sentiment]:
        self.generic_articles()
        with ThreadPoolExecutor(max_workers=self.cfg.get("workers", 4)) as pool:
            arts = dict(zip(names, pool.map(self.player_articles, names)))
        return {n: self.score(n, a) for n, a in arts.items()}
