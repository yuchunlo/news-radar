#!/usr/bin/env python3
"""Aggregate updates from OPML RSS subscriptions."""

from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
import html as html_mod
import json
import jsonio
import re
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
import requests
from dateutil import parser as dtparser

try:
    import feedparser
except ModuleNotFoundError:
    feedparser = None

try:
    from bs4 import BeautifulSoup
except ModuleNotFoundError:
    BeautifulSoup = None

UTC = timezone.utc
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# ---- Feed fetching tuning ---------------------------------------------------
# Split timeouts: a host that never completes the TCP/TLS handshake is dead and
# there is no point waiting the full read timeout for it.
FEED_CONNECT_TIMEOUT = 5      # [tune] seconds to establish the connection
FEED_READ_TIMEOUT = 12        # [tune] seconds to receive the feed body
FEED_MAX_WORKERS = 32         # [tune] concurrent feed fetches
# Feed entries usually carry the article body (or a long excerpt) in
# <content:encoded> / <description>. Keeping a capped copy on new items lets
# summarize_feed.py fall back to it when the page itself can't be fetched.
FEED_CONTENT_MAX_CHARS = 60000  # [tune] 0 disables capturing feed content
FEED_CONTENT_SKIP_HOSTS: tuple[str, ...] = (
    "youtube.com",
    "youtu.be",
    "soundon.fm",
    "firstory.me",
    "xiaoyuzhoufm.com",
)

RSS_FEED_REPLACEMENTS: dict[str, str] = {
    "https://rsshub.app/infoq/recommend": "https://www.infoq.cn/feed",
    "https://rsshub.app/huggingface/blog-zh": "https://huggingface.co/blog/feed.xml",
    "https://rsshub.app/readhub/daily": "https://readhub.cn/rss",
    "https://rsshub.app/36kr/hot-list": "https://36kr.com/feed",
    "https://rsshub.app/sspai/index": "https://sspai.com/feed",
    "https://rsshub.app/sspai/matrix": "https://sspai.com/feed",
    "https://rsshub.app/meituan/tech": "https://tech.meituan.com/feed",
    "https://mjg59.dreamwidth.org/data/rss": "http://mjg59.dreamwidth.org/data/rss",
}

RSS_FEED_SKIP_PREFIXES: tuple[str, ...] = (
    "https://rsshub.app/telegram/channel/",
    "https://rsshub.app/jike/",
    "https://rsshub.app/bilibili/",
    "https://rsshub.app/zhihu/",
    "https://rsshub.app/xiaoyuzhou/podcast/",
    "https://rsshub.app/xyzrank",
    "https://rsshub.app/mittrchina/hot",
    "https://wechat2rss.bestblogs.dev/",
    "https://werss.bestblogs.dev/",
    "http://47.122.94.119:18080/",
)

RSS_FEED_SKIP_EXACT: set[str] = {
    "https://rachelbythebay.com/w/atom.xml",
    "https://flak.tedunangst.com/rss",
}



@dataclass
class RawItem:
    site_id: str
    site_name: str
    source: str
    title: str
    url: str
    published_at: datetime | None
    meta: dict[str, Any]
    content: str = field(default="")


@dataclass
class FeedFetchResult:
    """單一個 feed 這次抓取的結果，交給 fetch_opml_rss 的主執行緒統一彙整
    （新項目、失敗訊息）。"""
    feed_title: str
    feed_url: str
    items: list[RawItem]
    error: str | None


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def iso(dt: datetime | None) -> str | None:
    if not dt:
        return None
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    try:
        dt = dtparser.parse(dt_str)
    except Exception:
        return None
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def normalize_url(raw_url: str) -> str:
    try:
        parsed = urlparse((raw_url or "").strip())
        if not parsed.scheme:
            return (raw_url or "").strip()
        query = []
        for k, v in parse_qsl(parsed.query, keep_blank_values=True):
            lk = k.lower()
            if lk.startswith("utm_"):
                continue
            if lk in {
                "ref", "spm", "fbclid", "gclid", "igshid", "mkt_tok",
                "mc_cid", "mc_eid", "_hsenc", "_hsmi",
            }:
                continue
            query.append((k, v))
        parsed = parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            fragment="",
            query=urlencode(query, doseq=True),
        )
        return urlunparse(parsed).rstrip("/")
    except Exception:
        return (raw_url or "").strip()


def host_of_url(raw_url: str) -> str:
    try:
        return urlparse(raw_url).netloc.lower()
    except Exception:
        return ""


def host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


VOCUS_AUTHOR_RE = re.compile(
    r"^(https?://(?:www\.)?vocus\.cc)/@[^/]+/([0-9A-Za-z]+)(.*)$"
)

# Feeds that publish their <link> elements with a development origin instead of
# the public one. NotesByLex.com serves every article link as
# http://localhost:8000/..., and follow.opml's htmlUrl says the same; from any
# other machine those addresses resolve to nothing, so summarize_feed can only
# ever fail on them.
#
# A dev origin tells us the address is *wrong*, but not what it should have
# been -- that needs a second, independent fact, and there are two sources for
# one:
#
#   1. FEED_ORIGIN_FIXUPS, keyed by OPML source name. Checked first, because it
#      is the deliberate statement and it is the only one available when
#      re-canonicalising a stored record: meta is never persisted into
#      archive.json (verified: 0 of 21,495 records carry it).
#   2. meta["feed_url"], the resolved xmlUrl. The convenient default: a feed's
#      own address must be reachable or we could not have fetched it, so a
#      future feed with this same bug is fixed with no configuration at all.
#      Not always right on its own -- an OPML entry can point at an aggregator
#      or bridge (rsshub.app, t.me) whose domain is not the article's domain,
#      which is what the table above is for.
#
# Never guess from the url alone. Leaving a localhost url alone fails visibly;
# rewriting it to the wrong host silently files one site's articles under
# another.
FEED_ORIGIN_FIXUPS = {
    "notesbylex.com": "https://notesbylex.com",
}
# Non-routable / development hosts. Any of these means "this url cannot have
# been meant for publication".
DEV_ORIGIN_RE = re.compile(
    r"^(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|[^.]+\.local)(?::\d+)?$",
    re.I,
)


def canonical_url(raw_url: str, source: str = "", feed_url: str = "") -> str:
    """Host-specific canonical form, on top of normalize_url's tracking-param
    stripping.

    `source` (OPML source name) and `feed_url` (the resolved xmlUrl) are both
    optional so that callers holding only a url keep working. The dev-origin
    fixup needs at least one of them; with neither, the url is left as it is
    rather than rewritten to a guess.
    """
    url = normalize_url(raw_url)
    fixed = fix_dev_origin(url, source=source, feed_url=feed_url)
    if fixed:
        return fixed
    m = VOCUS_AUTHOR_RE.match(url)
    if m:
        return f"{m.group(1)}/article/{m.group(2)}{m.group(3)}"
    return url


def public_origin(feed_url: str) -> str:
    """Origin of a feed's own address, when that address is itself publishable.

    Returns "" for an empty, unparseable, or dev-origin feed url -- a feed
    served from localhost gives us no public origin to copy.
    """
    try:
        parsed = urlparse((feed_url or "").strip())
    except Exception:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    if DEV_ORIGIN_RE.match(parsed.netloc):
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def fix_dev_origin(url: str, source: str = "", feed_url: str = "") -> str | None:
    """Rewrite a development origin to the public one for a known feed.

    Uses the FEED_ORIGIN_FIXUPS entry for `source` when there is one, otherwise
    the origin derived from `feed_url`. Returns None when the url is not on a
    dev origin, or when neither fact is available -- so this is safe to call on
    every url.

    The explicit entry wins on purpose. A feed's address is usually on the same
    domain as its articles, but not always: an OPML entry can point at an
    aggregator or bridge (rsshub.app, t.me), and deriving from those would move
    the articles onto the proxy's domain. The derived origin is the convenient
    default, the table is the override for when it would be wrong.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if not parsed.netloc or not DEV_ORIGIN_RE.match(parsed.netloc):
        return None
    origin = (FEED_ORIGIN_FIXUPS.get((source or "").strip().casefold(), "")
              or public_origin(feed_url))
    if not origin:
        return None
    good = urlparse(origin)
    rebuilt = parsed._replace(scheme=good.scheme, netloc=good.netloc)
    return urlunparse(rebuilt).rstrip("/")


def first_non_empty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        s = str(value).strip()
        if s:
            return s
    return ""


def make_item_id(site_id: str, source: str, title: str, url: str) -> str:
    key = "||".join([
        site_id.strip().lower(),
        source.strip().lower(),
        title.strip().lower(),
        normalize_url(url),
    ])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def parse_unix_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        n = float(value)
    except Exception:
        return None
    if n > 10_000_000_000:
        n /= 1000.0
    try:
        return datetime.fromtimestamp(n, tz=UTC)
    except Exception:
        return None


def parse_date_any(value: Any, now: datetime) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, (int, float)):
        return parse_unix_timestamp(value)
    s = str(value).strip()
    if not s:
        return None
    if s.startswith("$D"):
        s = s[2:]
    if re.fullmatch(r"\d{12,}", s):
        return parse_unix_timestamp(int(s))
    if re.fullmatch(r"\d{9,11}", s):
        return parse_unix_timestamp(int(s))
    try:
        dt = dtparser.parse(s, tzinfos={"UT": 0, "UTC": 0, "GMT": 0})
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


def parse_feed_entries_via_xml(feed_xml: bytes) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    try:
        root = ET.fromstring(feed_xml)
    except Exception:
        return out
    for tag in (".//{*}item", ".//{*}entry"):
        for node in root.findall(tag):
            title = (node.findtext("{*}title") or "").strip()
            link = ""
            link_node = node.find("{*}link")
            if link_node is not None:
                link = (link_node.get("href") or link_node.text or "").strip()
            published = (
                node.findtext("{*}pubDate")
                or node.findtext("{*}published")
                or node.findtext("{*}updated")
            )
            if title and link:
                key = (title, link)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"title": title, "link": link, "published": published})
    return out


def parse_opml_subscriptions(opml_path: Path) -> list[dict[str, str]]:
    root = ET.parse(opml_path).getroot()
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for outline in root.findall(".//outline"):
        xml_url = str(outline.attrib.get("xmlUrl") or "").strip()
        if not xml_url or xml_url in seen:
            continue
        seen.add(xml_url)
        title = first_non_empty(
            outline.attrib.get("title"),
            outline.attrib.get("text"),
            host_of_url(xml_url),
            xml_url,
        )
        out.append({
            "title": title,
            "category": str(outline.attrib.get("category") or "").strip(),
            "xml_url": xml_url,
            "html_url": str(outline.attrib.get("htmlUrl") or "").strip(),
        })
    return out


def resolve_official_rss_url(feed_url: str) -> tuple[str | None, str | None]:
    src = (feed_url or "").strip()
    if not src:
        return None, "empty_url"
    if src in RSS_FEED_SKIP_EXACT:
        return None, "no_official_rss_or_unreachable"
    for prefix in RSS_FEED_SKIP_PREFIXES:
        if src.startswith(prefix):
            return None, "no_official_rss_for_source_type"
    replaced = RSS_FEED_REPLACEMENTS.get(src)
    if replaced:
        return replaced, "official_replacement"
    return src, None


def resolve_opml_bridge_source(feed_url: str, html_url: str = "") -> dict[str, str] | None:
    src = (feed_url or "").strip()
    parsed = urlparse(src)
    parts = [p for p in parsed.path.strip("/").split("/") if p]

    if parsed.netloc == "rsshub.app" and len(parts) >= 3 and parts[:2] == ["telegram", "channel"]:
        slug = parts[2]
        return {
            "bridge_type": "telegram",
            "bridge_slug": slug,
            "url": f"https://t.me/s/{slug}",
        }

    if parsed.netloc == "rsshub.app" and len(parts) >= 3 and parts[0] == "jike":
        kind, ident = parts[1], parts[2]
        if kind == "topic":
            return {
                "bridge_type": "jike",
                "bridge_kind": "topic",
                "bridge_slug": ident,
                "url": f"https://m.okjike.com/topics/{ident}",
            }
        if kind == "user":
            return {
                "bridge_type": "jike",
                "bridge_kind": "user",
                "bridge_slug": ident,
                "url": f"https://m.okjike.com/users/{ident}",
            }

    html = (html_url or "").strip()
    if html.startswith("https://t.me/s/"):
        slug = html.rstrip("/").split("/")[-1]
        return {"bridge_type": "telegram", "bridge_slug": slug, "url": html}
    if html.startswith("https://m.okjike.com/topics/"):
        ident = html.rstrip("/").split("/")[-1]
        return {"bridge_type": "jike", "bridge_kind": "topic", "bridge_slug": ident, "url": html}
    if html.startswith("https://m.okjike.com/users/"):
        ident = html.rstrip("/").split("/")[-1]
        return {"bridge_type": "jike", "bridge_kind": "user", "bridge_slug": ident, "url": html}

    return None


def compact_title(text: str, limit: int = 96) -> str:
    s = re.sub(r"\s+", " ", text or "").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def parse_telegram_public_items(
    html: str,
    *,
    now: datetime,
    source_name: str,
    slug: str,
    site_id: str = "opmlrss",
) -> list[RawItem]:
    if BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out: list[RawItem] = []
    for msg in soup.select(".tgme_widget_message"):
        data_post = str(msg.get("data-post") or "").strip()
        if not data_post:
            continue
        text_node = msg.select_one(".tgme_widget_message_text")
        text = text_node.get_text(" ", strip=True) if text_node else ""
        if not text:
            preview = msg.select_one(".tgme_widget_message_link_preview_title")
            text = preview.get_text(" ", strip=True) if preview else ""
        if not text:
            continue
        time_node = msg.select_one("time[datetime]")
        published = parse_date_any(time_node.get("datetime") if time_node else None, now)
        if not published:
            continue
        out.append(RawItem(
            site_id=site_id, site_name="OPML RSS", source=source_name,
            title=compact_title(text), url=f"https://t.me/{data_post}",
            published_at=published,
            meta={"bridge_type": "telegram", "bridge_slug": slug,
                  "feed_home": f"https://t.me/s/{slug}", "opml_category": site_id},
        ))
    return out


def parse_jike_public_items(
    html: str,
    *,
    now: datetime,
    source_name: str,
    source_url: str,
    site_id: str = "opmlrss",
) -> list[RawItem]:
    if BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None or not script.string:
        return []
    try:
        payload = json.loads(script.string)
    except Exception:
        return []
    posts = payload.get("props", {}).get("pageProps", {}).get("posts") or []
    out: list[RawItem] = []
    for post in posts:
        if not isinstance(post, dict):
            continue
        post_id = str(post.get("id") or "").strip()
        text = str(post.get("content") or "").strip()
        if not post_id or not text:
            continue
        published = parse_date_any(post.get("createdAt") or post.get("actionTime"), now)
        if not published:
            continue
        out.append(RawItem(
            site_id=site_id, site_name="OPML RSS", source=source_name,
            title=compact_title(text),
            url=f"https://m.okjike.com/originalPosts/{post_id}",
            published_at=published,
            meta={"bridge_type": "jike", "feed_home": source_url, "opml_category": site_id},
        ))
    return out


def build_http_session() -> requests.Session:
    """A session tuned for "fetch a few hundred feeds once, quickly".

    The retry policy is deliberately minimal. The previous
    ``Retry(total=2, backoff_factor=0.5, status_forcelist=(500, 502, 503, 504))``
    multiplied the cost of every *unhealthy* feed by three full read timeouts
    plus backoff, and because the run finishes only when the slowest worker
    does, those feeds set the wall-clock time for the whole step. Feeds are
    re-fetched on the next scheduled run anyway, so a feed that is down right
    now gains nothing from being asked three times in a row.

    ``connect`` retries are kept (a refused connection or a DNS blip fails in
    milliseconds, so retrying it is nearly free) while ``read=False`` makes a
    read timeout surface immediately instead of being retried.
    """
    session = requests.Session()
    retry = Retry(
        total=1,
        connect=1,
        read=False,
        status=0,
        backoff_factor=0.2,
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=FEED_MAX_WORKERS,
        pool_maxsize=FEED_MAX_WORKERS,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": BROWSER_UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    })
    return session


FEED_CONTENT_DROP_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.S | re.I)
FEED_CONTENT_BREAK_RE = re.compile(
    r"</(?:p|div|li|tr|h[1-6]|blockquote|section)\s*>|<br\s*/?>", re.I
)
FEED_CONTENT_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(raw: str) -> str:
    """Flatten feed markup into plain text, keeping block boundaries as
    newlines so the summarizer can still split it into sentences."""
    if not raw:
        return ""
    text = FEED_CONTENT_DROP_RE.sub(" ", raw)
    text = FEED_CONTENT_BREAK_RE.sub("\n", text)
    text = FEED_CONTENT_TAG_RE.sub("", text)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def feed_entry_content(entry: Any, link: str = "") -> str:
    """The longest body-ish payload a feed entry offers, as capped plain text.

    Many feeds (especially the third-party bridges in this OPML) ship the whole
    article in <content:encoded>. Keeping it means a page that later refuses to
    be fetched — paywall, anti-bot, 404 after deletion — can still be
    summarized instead of staying pending forever.
    """
    if FEED_CONTENT_MAX_CHARS <= 0:
        return ""
    if host_matches(host_of_url(link), FEED_CONTENT_SKIP_HOSTS):
        return ""
    candidates: list[str] = []
    try:
        for block in (entry.get("content") or []):
            if isinstance(block, dict):
                candidates.append(str(block.get("value") or ""))
    except Exception:
        pass
    for key in ("summary", "description", "subtitle"):
        try:
            value = entry.get(key)
        except Exception:
            value = None
        if value:
            candidates.append(str(value))
    if not candidates:
        return ""
    text = html_to_text(max(candidates, key=len))
    return text[:FEED_CONTENT_MAX_CHARS]


_thread_state = threading.local()


def thread_session() -> requests.Session:
    """One session per worker thread.

    A single shared session works, but its connection pool is a process-wide
    structure that all workers contend on, and with ~200 distinct feed hosts
    the per-host pool cache is evicted constantly, so keep-alive rarely pays
    off. A session per thread keeps its own pools, so a worker that handles
    several feeds from the same host (115 of these feeds are YouTube) reuses
    the connection it already has.
    """
    session = getattr(_thread_state, "session", None)
    if session is None:
        session = build_http_session()
        _thread_state.session = session
    return session


def fetch_opml_rss(
    now: datetime,
    opml_path: Path,
    max_feeds: int = 0,
) -> tuple[list[RawItem], list[tuple[str, str, str]]]:
    feeds = parse_opml_subscriptions(opml_path)
    if max_feeds > 0:
        feeds = feeds[:max_feeds]

    out: list[RawItem] = []
    resolved_feeds: list[dict[str, str]] = []

    for feed in feeds:
        original_url = feed["xml_url"]
        bridge = resolve_opml_bridge_source(original_url, feed.get("html_url") or "")
        if bridge:
            record = dict(feed)
            record["xml_url_original"] = original_url
            record["xml_url"] = bridge["url"]
            record.update(bridge)
            resolved_feeds.append(record)
            continue

        resolved_url, _ = resolve_official_rss_url(original_url)
        if not resolved_url:
            continue
        record = dict(feed)
        record["xml_url_original"] = original_url
        record["xml_url"] = resolved_url
        resolved_feeds.append(record)

    # Several OPML entries can resolve to the same feed (the RSS_FEED_REPLACEMENTS
    # table maps both sspai/index and sspai/matrix to sspai.com/feed, for
    # instance). Fetch each distinct URL once and hand the same response to
    # every OPML entry that wanted it.
    fetch_groups: dict[str, list[dict[str, str]]] = {}
    for record in resolved_feeds:
        fetch_groups.setdefault(record["xml_url"], []).append(record)

    def parse_for_feed(feed: dict[str, str], resp: requests.Response) -> list[RawItem]:
        """Turn one fetched response into RawItems for one OPML entry."""
        feed_url = feed["xml_url"]
        feed_title = feed["title"]
        feed_category = str(feed.get("category") or "opmlrss").strip() or "opmlrss"
        base_meta = {
            "feed_url": feed_url,
            "feed_home": feed.get("html_url") or "",
            "opml_category": feed_category,
        }
        bridge_type = str(feed.get("bridge_type") or "")

        if bridge_type == "telegram":
            return parse_telegram_public_items(
                resp.text,
                now=now,
                source_name=feed_title,
                slug=str(feed.get("bridge_slug") or ""),
                site_id=feed_category,
            )
        if bridge_type == "jike":
            return parse_jike_public_items(
                resp.text,
                now=now,
                source_name=feed_title,
                source_url=feed_url,
                site_id=feed_category,
            )

        local_items: list[RawItem] = []
        if feedparser is not None:
            parsed = feedparser.parse(resp.content)
            source_name = first_non_empty(
                feed_title,
                getattr(parsed, "feed", {}).get("title"),
                host_of_url(feed_url),
            )
            for entry in parsed.entries:
                title = str(entry.get("title", "")).strip()
                link = str(entry.get("link", "")).strip()
                if not title or not link:
                    continue
                published = (
                    parse_date_any(entry.get("published"), now)
                    or parse_date_any(entry.get("updated"), now)
                    or parse_date_any(entry.get("pubDate"), now)
                )
                if not published:
                    continue
                local_items.append(
                    RawItem(
                        site_id=feed_category,
                        site_name="OPML RSS",
                        source=source_name,
                        title=title,
                        url=link,
                        published_at=published,
                        meta=dict(base_meta),
                        content=feed_entry_content(entry, link),
                    )
                )
            return local_items

        source_name = first_non_empty(feed_title, host_of_url(feed_url))
        for entry in parse_feed_entries_via_xml(resp.content):
            published = parse_date_any(entry.get("published"), now)
            if not published:
                continue
            local_items.append(
                RawItem(
                    site_id=feed_category,
                    site_name="OPML RSS",
                    source=source_name,
                    title=entry.get("title", ""),
                    url=entry.get("link", ""),
                    published_at=published,
                    meta=dict(base_meta),
                    content=(
                        ""
                        if host_matches(host_of_url(entry.get("link", "")),
                                        FEED_CONTENT_SKIP_HOSTS)
                        else html_to_text(entry.get("description", ""))[:FEED_CONTENT_MAX_CHARS]
                    ),
                )
            )
        return local_items

    def fetch_single_url(feed_url: str, group: list[dict[str, str]]) -> FeedFetchResult:
        feed_title = group[0]["title"]
        try:
            resp = thread_session().get(
                feed_url, timeout=(FEED_CONNECT_TIMEOUT, FEED_READ_TIMEOUT)
            )
            resp.raise_for_status()
            items: list[RawItem] = []
            for feed in group:
                items.extend(parse_for_feed(feed, resp))
            resp.close()
            return FeedFetchResult(
                feed_title=feed_title, feed_url=feed_url, items=items, error=None,
            )
        except Exception as e:
            return FeedFetchResult(
                feed_title=feed_title, feed_url=feed_url,
                items=[], error=f"{type(e).__name__}: {e}",
            )

    fetch_errors: list[tuple[str, str, str]] = []
    if fetch_groups:
        worker_count = min(FEED_MAX_WORKERS, max(4, len(fetch_groups)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(fetch_single_url, url, group)
                for url, group in fetch_groups.items()
            ]
            for future in as_completed(futures):
                result = future.result()
                out.extend(result.items)
                if result.error:
                    fetch_errors.append((result.feed_title, result.feed_url, result.error))

    if fetch_errors:
        print(f"WARNING: {len(fetch_errors)} feed(s) failed to fetch:")
        for feed_title, feed_url, error in fetch_errors:
            print(f"  - [{feed_title}] {feed_url}: {error}")

    return out, fetch_errors


# ---------------------------------------------------------------- Duplicate merging

# Two records describing the same post can end up with different ids, because
# the id is a hash that includes the title and feeds do edit titles after
# publishing (adding or dropping punctuation, fixing a typo). Anything that
# agrees on all three of these fields is the same post.
DEDUPE_KEY_FIELDS: tuple[str, ...] = ("published_at", "source", "url")

# Identity fields, plus fields whose own merge rule is handled explicitly.
MERGE_HANDLED_FIELDS = frozenset(
    DEDUPE_KEY_FIELDS + ("id", "summary", "first_seen_at", "last_seen_at")
)

FALLBACK_MARK = "↛"


def is_blank(value: Any) -> bool:
    """True for "no value here": missing, None, empty string or empty
    container. 0 and False are real values and are not blank."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple, set)):
        return not value
    return False


def better_summary(current: str | None, other: str | None) -> str | None:
    """Prefer a real summary over a fallback-marked one (blocked page, meta
    description only); between two of the same kind, prefer the longer."""
    if is_blank(current):
        return other
    if is_blank(other):
        return current
    cur_fallback = FALLBACK_MARK in current
    oth_fallback = FALLBACK_MARK in other
    if cur_fallback != oth_fallback:
        return other if cur_fallback else current
    return other if len(other) > len(current) else current


def absorb_item(keeper: dict[str, Any], other: dict[str, Any]) -> None:
    """Fold `other`'s values into `keeper` without overwriting anything
    `keeper` already knows (thumbnail, summary, scores, ...)."""
    merged_summary = better_summary(keeper.get("summary"), other.get("summary"))
    if not is_blank(merged_summary):
        keeper["summary"] = merged_summary
    for field_name, pick in (("first_seen_at", min), ("last_seen_at", max)):
        values = [v for v in (keeper.get(field_name), other.get(field_name)) if v]
        if values:
            keeper[field_name] = pick(values)
    for key, value in other.items():
        if key in MERGE_HANDLED_FIELDS:
            continue
        if is_blank(keeper.get(key)) and not is_blank(value):
            keeper[key] = value


def merge_duplicate_items(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Collapse records that agree on every DEDUPE_KEY_FIELDS value.

    The surviving record is the one that came first in the file — the archive is
    written newest-`last_seen_at`-first, so that is the most recently seen copy
    — and the others are folded into it before being dropped. Records with an
    incomplete key are never merged, only compared records can be.
    """
    groups: dict[tuple, list[int]] = {}
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        key = tuple(item.get(f) for f in DEDUPE_KEY_FIELDS)
        if any(is_blank(part) for part in key):
            continue
        groups.setdefault(key, []).append(idx)

    dropped: set[int] = set()
    for _key, idxs in groups.items():
        if len(idxs) < 2:
            continue
        idxs_sorted = sorted(idxs)
        keeper = items[idxs_sorted[0]]
        for idx in idxs_sorted[1:]:
            absorb_item(keeper, items[idx])
            dropped.add(idx)

    if not dropped:
        return items, 0
    kept = [item for idx, item in enumerate(items) if idx not in dropped]
    return kept, len(dropped)


def migrate_record_urls(records: list[dict[str, Any]]) -> int:
    """Re-canonicalise urls already in the archive, and re-key the records whose
    url changed.

    Without this, changing canonical_url would fork every affected item: the
    next run stores the new address under a new id hash and the old record just
    sits there until it ages out. Re-keying instead makes the two collapse into
    one during merge_duplicate_items. The id is only recomputed when the url
    actually changed, so subtitle files named after an item id (see
    download_sub.py) keep matching.
    """
    changed = 0
    for record in records:
        old_url = str(record.get("url") or "")
        if not old_url:
            continue
        new_url = canonical_url(old_url, str(record.get("source") or ""))
        if new_url == old_url:
            continue
        if not new_url:
            continue
        record["url"] = new_url
        record["id"] = make_item_id(
            str(record.get("site_id") or ""),
            str(record.get("source") or ""),
            str(record.get("title") or "").strip(),
            new_url,
        )
        changed += 1
    return changed


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = jsonio.dumps(payload)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def load_archive(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except Exception as e:
        raise SystemExit(
            f"ERROR: {path} exists but is not valid JSON ({e}).\n"
            f"       Size on disk: {len(raw)} chars. Refusing to continue, "
            f"because writing now would discard the whole archive.\n"
            f"       Restore it (`git checkout -- {path}`) or delete it "
            f"deliberately to start over."
        )
    items = payload.get("items", [])
    records: list[dict[str, Any]] = []
    if isinstance(items, list):
        records = [it for it in items if isinstance(it, dict)]
    elif isinstance(items, dict):
        for item_id, it in items.items():
            if isinstance(it, dict):
                it["id"] = item_id
                records.append(it)

    migrated = migrate_record_urls(records)
    if migrated:
        print(f"Re-canonicalised {migrated} stored url(s).")

    records, merged = merge_duplicate_items(records)
    if merged:
        print(f"Merged {merged} duplicate item(s) by "
              f"{' + '.join(DEDUPE_KEY_FIELDS)}.")

    out: dict[str, dict[str, Any]] = {}
    for it in records:
        if it.get("id"):
            out[it["id"]] = it
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate OPML RSS updates")
    parser.add_argument("--output-dir", default="data", help="Directory for output JSON files")
    parser.add_argument("--archive-days", type=int, default=210, help="Keep archive for N days")
    parser.add_argument("--rss-opml", default="", help="Optional OPML file path to include RSS sources")
    parser.add_argument("--rss-max-feeds", type=int, default=0, help="Optional max OPML RSS feeds to fetch (0 means all)")
    parser.add_argument(
        "--feed-content-preview", type=int, default=40,
        help="Chars of each captured feed body to print in the end-of-run "
             "report (0 = don't print the report, -1 = print it in full)",
    )
    args = parser.parse_args(argv)

    now = utc_now()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / "archive.json"

    archive = load_archive(archive_path)

    raw_items: list[RawItem] = []
    if args.rss_opml:
        opml_path = Path(args.rss_opml).expanduser()
        if opml_path.exists():
            raw_items, _fetch_errors = fetch_opml_rss(
                now, opml_path,
                max_feeds=max(0, int(args.rss_max_feeds)),
            )
        else:
            print(f"WARNING: OPML not found: {opml_path}")
    else:
        print("WARNING: no --rss-opml provided; nothing to fetch.")

    captured_feed_content: list[tuple[str, str]] = []

    for raw in raw_items:
        title = raw.title.strip()
        url = canonical_url(raw.url, raw.source,
                            str((raw.meta or {}).get("feed_url") or ""))
        if not title or not url or not url.startswith("http"):
            continue
        item_id = make_item_id(raw.site_id, raw.source, title, url)
        existing = archive.get(item_id)
        if existing is None:
            new_item = {
                "id": item_id,
                "site_id": raw.site_id,
                "site_name": raw.site_name,
                "source": raw.source,
                "title": title,
                "url": url,
                "published_at": iso(raw.published_at),
                "first_seen_at": iso(now),
                "last_seen_at": iso(now),
            }
            if raw.content:
                new_item["feed_content"] = raw.content
                captured_feed_content.append((url, raw.content))
            archive[item_id] = new_item
        else:
            # Keep the feed's own copy of the body only while it is still
            # useful, i.e. until the item has a summary.
            if existing.get("summary"):
                existing.pop("feed_content", None)
            elif raw.content and not existing.get("feed_content"):
                existing["feed_content"] = raw.content
                captured_feed_content.append((url, raw.content))
            existing["site_id"] = raw.site_id
            existing["site_name"] = raw.site_name
            existing["source"] = raw.source
            existing["title"] = title
            existing["url"] = url
            if raw.published_at:
                # OPML RSS may fix previously wrong publish times; allow overwrite.
                if raw.site_id == "opmlrss" or not existing.get("published_at"):
                    existing["published_at"] = iso(raw.published_at)
            existing["last_seen_at"] = iso(now)

    # Prune old archive
    keep_after = now - timedelta(days=args.archive_days)
    pruned: dict[str, dict[str, Any]] = {}
    for item_id, record in archive.items():
        ts = (parse_iso(record.get("last_seen_at"))
              or parse_iso(record.get("published_at"))
              or parse_iso(record.get("first_seen_at")) or now)
        if ts >= keep_after:
            if record.get("summary"):
                record.pop("feed_content", None)
            pruned[item_id] = record
    archive = pruned

    archive_payload = {
        "generated_at": iso(now),
        "total_items": len(archive),
        "items": sorted(
            archive.values(),
            key=lambda x: parse_iso(x.get("last_seen_at")) or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        ),
    }
    # Indented, to match what summarize_feed.py writes back to the same file:
    # if the two disagreed, every run would rewrite the whole file in the other
    # format and produce a full-file diff.
    write_json_atomic(archive_path, archive_payload)
    print(f"Wrote: {archive_path} ({len(archive)} items, fetched {len(raw_items)} raw)")

    # Reported here rather than inside the ingest loop, so the loop's own
    # output stays a clean one-line-per-feed log and the bodies are all in one
    # block at the end.
    if captured_feed_content and args.feed_content_preview != 0:
        preview = args.feed_content_preview
        print(f"\nFeed content captured for {len(captured_feed_content)} item(s):")
        for url, content in captured_feed_content:
            flat = re.sub(r"\s+", " ", content).strip()
            if preview > 0 and len(flat) > preview:
                flat = flat[:preview] + f"… (+{len(content) - preview} chars)"
            print(f"  {url}\n    {flat}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
