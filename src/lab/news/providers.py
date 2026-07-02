"""News retrieval for M3: Google News RSS per market + configurable feeds.

`NewsProvider` is the one pluggable seam the brief allows. Articles carry
their publish timestamp so the aggregator can enforce no-look-ahead
(published_ts <= forecast ts, guardrail 11).
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

STOPWORDS = {
    "will", "the", "a", "an", "by", "in", "on", "at", "of", "to", "be", "is",
    "before", "after", "than", "or", "and", "for", "with", "does", "do",
}


@dataclass
class Article:
    title: str
    url: str
    source: str
    published_ts: str | None  # ISO-8601 UTC
    summary: str = ""


class NewsProvider(Protocol):
    def fetch(self, query: str, max_items: int = 20) -> list[Article]: ...


def market_query(question: str) -> str:
    """Keyword query from the market question: drop stopwords and dates."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9'-]+", question)
    keep = [w for w in words if w.lower() not in STOPWORDS][:8]
    return " ".join(keep)


def _parse_rss(xml_text: str, source: str, max_items: int) -> list[Article]:
    articles: list[Article] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        log.warning("news: unparseable RSS", extra={"ctx": {"source": source}})
        return articles
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        pub = item.findtext("pubDate")
        published = None
        if pub:
            try:
                published = parsedate_to_datetime(pub).astimezone(timezone.utc).isoformat(
                    timespec="seconds"
                )
            except (ValueError, TypeError):
                pass
        desc = (item.findtext("description") or "").strip()
        if title and url:
            articles.append(Article(title=title, url=url, source=source,
                                    published_ts=published, summary=desc[:500]))
        if len(articles) >= max_items:
            break
    return articles


class GoogleNewsRss:
    BASE = "https://news.google.com/rss/search"

    def __init__(self, timeout: float = 15.0) -> None:
        self._timeout = timeout

    def fetch(self, query: str, max_items: int = 20) -> list[Article]:
        url = f"{self.BASE}?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"
        try:
            resp = httpx.get(url, timeout=self._timeout, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError:
            log.warning("news: google rss fetch failed", extra={"ctx": {"query": query}})
            return []
        return _parse_rss(resp.text, "google-news", max_items)


class FeedListProvider:
    """Static RSS feed list from config; filtered by query keywords in title."""

    def __init__(self, feed_urls: list[str], timeout: float = 15.0) -> None:
        self.feed_urls = feed_urls
        self._timeout = timeout

    def fetch(self, query: str, max_items: int = 20) -> list[Article]:
        keywords = {w.lower() for w in query.split()}
        out: list[Article] = []
        for feed in self.feed_urls:
            try:
                resp = httpx.get(feed, timeout=self._timeout, follow_redirects=True)
                resp.raise_for_status()
            except httpx.HTTPError:
                log.warning("news: feed fetch failed", extra={"ctx": {"feed": feed}})
                continue
            for a in _parse_rss(resp.text, feed, max_items):
                title_words = set(re.findall(r"[a-z0-9'-]+", a.title.lower()))
                if keywords & title_words:
                    out.append(a)
        return out[:max_items]


def dedup_articles(articles: list[Article]) -> list[Article]:
    seen: set[str] = set()
    out: list[Article] = []
    for a in articles:
        if a.url not in seen:
            seen.add(a.url)
            out.append(a)
    return out


def gather_news(question: str, providers: list[NewsProvider], max_items: int = 20) -> list[Article]:
    query = market_query(question)
    collected: list[Article] = []
    for p in providers:
        try:
            collected.extend(p.fetch(query, max_items))
        except Exception:
            log.exception("news: provider failed")
    return dedup_articles(collected)[:max_items]
