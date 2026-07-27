#!/usr/bin/env python3
"""Aggregate updates from OPML RSS subscriptions."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
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
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
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

    session = build_http_session()

    def fetch_single_feed(feed: dict[str, str]) -> FeedFetchResult:
        feed_url = feed["xml_url"]
        feed_title = feed["title"]
        feed_category = str(feed.get("category") or "opmlrss").strip() or "opmlrss"
        local_items: list[RawItem] = []
        try:
            headers = {
                "User-Agent": BROWSER_UA,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }

            resp = session.get(feed_url, timeout=12, headers=headers)
            resp.raise_for_status()

            bridge_type = str(feed.get("bridge_type") or "")
            if bridge_type == "telegram":
                local_items = parse_telegram_public_items(
                    resp.text,
                    now=now,
                    source_name=feed_title,
                    slug=str(feed.get("bridge_slug") or ""),
                    site_id=feed_category,
                )
            elif bridge_type == "jike":
                local_items = parse_jike_public_items(
                    resp.text,
                    now=now,
                    source_name=feed_title,
                    source_url=feed_url,
                    site_id=feed_category,
                )
            elif feedparser is not None:
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
                            meta={
                                "feed_url": feed_url,
                                "feed_home": feed.get("html_url") or "",
                                "opml_category": feed_category,
                            },
                        )
                    )
            else:
                source_name = first_non_empty(feed_title, host_of_url(feed_url))
                for entry in parse_feed_entries_via_xml(resp.content):
                    published = parse_date_any(entry.get("published"), now)
                    if not published:
                        continue
                    link = entry.get("link", "")
                    local_items.append(
                        RawItem(
                            site_id=feed_category,
                            site_name="OPML RSS",
                            source=source_name,
                            title=entry.get("title", ""),
                            url=link,
                            published_at=published,
                            meta={
                                "feed_url": feed_url,
                                "feed_home": feed.get("html_url") or "",
                                "opml_category": feed_category,
                            },
                        )
                    )

            return FeedFetchResult(
                feed_title=feed_title, feed_url=feed_url,
                items=local_items, error=None,
            )
        except Exception as e:
            return FeedFetchResult(
                feed_title=feed_title, feed_url=feed_url,
                items=[], error=f"{type(e).__name__}: {e}",
            )


    fetch_errors: list[tuple[str, str, str]] = []
    if resolved_feeds:
        worker_count = min(20, max(4, len(resolved_feeds)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(fetch_single_feed, f) for f in resolved_feeds]
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


def load_archive(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    items = payload.get("items", [])
    out: dict[str, dict[str, Any]] = {}
    if isinstance(items, list):
        for it in items:
            if it.get("id"):
                out[it["id"]] = it
    elif isinstance(items, dict):
        for item_id, it in items.items():
            if isinstance(it, dict):
                it["id"] = item_id
                out[item_id] = it
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate OPML RSS updates")
    parser.add_argument("--output-dir", default="data", help="Directory for output JSON files")
    parser.add_argument("--archive-days", type=int, default=210, help="Keep archive for N days")
    parser.add_argument("--rss-opml", default="", help="Optional OPML file path to include RSS sources")
    parser.add_argument("--rss-max-feeds", type=int, default=0, help="Optional max OPML RSS feeds to fetch (0 means all)")
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

    for raw in raw_items:
        title = raw.title.strip()
        url = normalize_url(raw.url)
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
                "star": False,
            }
            archive[item_id] = new_item
        else:
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
    archive_path.write_text(
        json.dumps(archive_payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"Wrote: {archive_path} ({len(archive)} items, fetched {len(raw_items)} raw)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
