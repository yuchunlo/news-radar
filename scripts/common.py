"""Shared primitives: archive I/O, host matching, HTTP, item sentinels."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urljoin, urlparse

import charset_normalizer

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


ID_RE = re.compile(r"[0-9a-f]{40}")          # sha1 from update_news.make_id; names files
LANG_RE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*")


def valid_id(item_id) -> bool:
    return isinstance(item_id, str) and bool(ID_RE.fullmatch(item_id))


def safe_lang(code, default="und") -> str:
    """A language code fit for a filename; anything else becomes `default`."""
    code = str(code or "").strip()
    return code if LANG_RE.fullmatch(code) and len(code) <= 35 else default


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
# Every url we fetch comes from third-party feeds, and whatever a page returns
# ends up in a public commit. So: http(s) only, public addresses only (checked
# again on every redirect hop), bounded body size.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
IMPERSONATE = os.environ.get("CURL_IMPERSONATE", "chrome")
MAX_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5


class Resp(NamedTuple):
    status: int
    content: bytes
    headers: dict
    url: str

    @property
    def text(self) -> str:
        enc = requests.utils.get_encoding_from_headers(self.headers)
        if not enc or enc.lower() in ("iso-8859-1", "ascii"):     # header absent or a lie
            best = charset_normalizer.from_bytes(self.content[:200_000]).best()
            enc = best.encoding if best else "utf-8"
        return self.content.decode(enc, errors="replace")


@lru_cache(maxsize=4096)
def _public_host(host: str, port: int) -> bool:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for *_, addr in infos:
        ip = ipaddress.ip_address(addr[0].split("%", 1)[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(infos)


def safe_url(url: str) -> bool:
    """http(s) to a host that resolves only to public addresses."""
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.hostname) and not p.username \
            and _public_host(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except ValueError:
        return False


def make_session(retries: int, headers: dict, *, read_retry=True,
                 status=(429, 500, 502, 503, 504), pool=16) -> requests.Session:
    s = requests.Session()
    retry = Retry(total=retries, connect=retries, redirect=0,
                  read=retries if read_retry else False,
                  status=retries if status else 0, status_forcelist=list(status),
                  backoff_factor=0.8 if status else 0.2,
                  allowed_methods=frozenset(["GET", "POST"]),
                  respect_retry_after_header=bool(status), raise_on_status=False)
    ad = HTTPAdapter(max_retries=retry, pool_connections=pool, pool_maxsize=pool)
    s.mount("http://", ad)
    s.mount("https://", ad)
    s.headers.update(headers)
    return s


def _read(r) -> bytes | None:
    if int(r.headers.get("Content-Length") or 0) > MAX_BYTES:
        return None
    buf = bytearray()
    for chunk in r.iter_content(65536):
        buf += chunk
        if len(buf) > MAX_BYTES:
            return None
    return bytes(buf)


def get(url: str, timeout, session: requests.Session | None = None,
        impersonate=False, lang="en") -> Resp | None:
    """GET with redirects followed by hand (each hop re-checked), or None when
    the url is unsafe, the body too large, the transport fails, or (with
    impersonate) curl_cffi is missing."""
    if impersonate and curl_requests is None:
        return None
    for _ in range(MAX_REDIRECTS + 1):
        if not safe_url(url):
            return None
        try:
            if impersonate:
                r = curl_requests.get(url, timeout=timeout, impersonate=IMPERSONATE, stream=True,
                                      headers={"Accept-Language": lang}, allow_redirects=False)
            else:
                r = (session or requests).get(url, timeout=timeout, stream=True, allow_redirects=False)
            try:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    url = urljoin(url, r.headers["Location"])
                    continue
                body = _read(r)
                return None if body is None else Resp(r.status_code, body, dict(r.headers), url)
            finally:
                r.close()
        except Exception:
            return None
    return None
