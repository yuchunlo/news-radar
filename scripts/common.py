"""Shared primitives: archive I/O, host matching, HTTP, item sentinels."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from curl_cffi import requests as curl_requests
except Exception:                      # optional; comes with yt-dlp[curl-cffi]
    curl_requests = None

# ---- summary sentinels ------------------------------------------------------
# key missing      -> pending, retried every run
# BLANK_SUMMARY    -> deliberately empty (douban mark, no speech); never retried
# GONE_SUMMARY     -> permanent placeholder (404/410, no archive); never retried
FALLBACK_MARK = "↛"
BLANK_SUMMARY = " "
GONE_SUMMARY = "無法取得頁面內容（原始頁面已移除，且無存檔）" + FALLBACK_MARK
PLACEHOLDER_PREFIX = "無法取得頁面內容"


def is_pending(item) -> bool:
    return isinstance(item, dict) and bool(item.get("url")) and not item.get("summary")


# ---- hosts ------------------------------------------------------------------
def host_of(url: str) -> str:
    try:
        return urlparse(url or "").netloc.lower()
    except Exception:
        return ""


def host_in(url: str, suffixes) -> bool:
    h = host_of(url)
    return any(h == s or h.endswith("." + s) for s in suffixes)


def is_youtube(url: str) -> bool:
    return host_in(url, ("youtube.com", "youtu.be"))


# ---- archive I/O: one line per top-level key, one line per item -------------
# Every writer of archive.json goes through here; a second format would make
# each script reflow the whole file and every commit a full-file diff.
_C = (",", ":")


def dumps(payload) -> str:
    j = lambda v: json.dumps(v, ensure_ascii=False, separators=_C)
    rows = lambda items, pad: ",\n".join(pad + j(it) for it in items)
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        head = [f" {j(k)}: {j(v)}," for k, v in payload.items() if k != "items"]
        parts = ["{", *head, ' "items": [', rows(payload["items"], "  "), " ]", "}"]
    elif isinstance(payload, list):
        parts = ["[", rows(payload, " "), "]"]
    else:
        return j(payload) + "\n"
    return "\n".join(p for p in parts if p) + "\n"


def write_atomic(path, payload) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dumps(payload))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_doc(path) -> dict:
    """Load an archive, refusing to continue on a corrupt file (writing now
    would discard everything)."""
    p = Path(path)
    if not p.exists():
        return {"items": []}
    raw = p.read_text(encoding="utf-8")
    try:
        doc = json.loads(raw)
    except Exception as e:
        raise SystemExit(f"ERROR: {p} is not valid JSON ({e}); restore it "
                         f"(`git checkout -- {p}`) or delete it deliberately.")
    if isinstance(doc, list):
        doc = {"items": doc}
    doc["items"] = [it for it in doc.get("items") or [] if isinstance(it, dict)]
    return doc


def save_doc(path, doc: dict) -> None:
    doc["total_items"] = len(doc["items"])
    write_atomic(path, doc)


# ---- HTTP -------------------------------------------------------------------
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
IMPERSONATE = os.environ.get("CURL_IMPERSONATE", "chrome")


def make_session(retries: int, headers: dict, *, read_retry=True,
                 status=(429, 500, 502, 503, 504), pool=16) -> requests.Session:
    s = requests.Session()
    retry = Retry(total=retries, connect=retries,
                  read=retries if read_retry else False,
                  status=retries if status else 0, status_forcelist=list(status),
                  backoff_factor=0.8 if status else 0.2,
                  allowed_methods=frozenset(["GET", "POST"]),
                  respect_retry_after_header=bool(status))
    ad = HTTPAdapter(max_retries=retry, pool_connections=pool, pool_maxsize=pool)
    s.mount("http://", ad)
    s.mount("https://", ad)
    s.headers.update(headers)
    return s


def curl_get(url: str, timeout: float, lang: str):
    """GET with a real Chrome TLS fingerprint, or None if curl_cffi is absent."""
    if curl_requests is None:
        return None
    return curl_requests.get(url, timeout=timeout, impersonate=IMPERSONATE,
                             headers={"Accept-Language": lang}, allow_redirects=True)
