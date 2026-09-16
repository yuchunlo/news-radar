#!/usr/bin/env python3

from __future__ import annotations

import os
import re
from pathlib import Path
import time
from urllib.parse import quote
from typing import Optional

try:
    import requests
    HAS_REQUESTS = True
except Exception:
    requests = None
    HAS_REQUESTS = False


try:
    from opencc import OpenCC
    _S2TWP = OpenCC("s2twp")
    _T2S = OpenCC("t2s")
    HAS_OPENCC = True
except Exception:
    _S2TWP = _T2S = None
    HAS_OPENCC = False


def _doubled_phrase_repairs() -> list[tuple[re.Pattern, str]]:
    if not HAS_OPENCC:
        return []
    try:
        import opencc as _oc
        path = Path(_oc.__file__).parent / "dictionary" / "TWPhrases.txt"
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            src, dst = parts[0], parts[1].split(" ")[0]
            if src and src != dst and src in dst:
                out.append((re.compile(re.escape(dst.replace(src, dst))), dst))
        return out
    except Exception:
        return []


_DOUBLED_REPAIRS = _doubled_phrase_repairs()


def repair_doubled_phrases(text: str) -> str:
    if not text:
        return text
    for pat, good in _DOUBLED_REPAIRS:
        prev = None
        while prev != text:
            prev = text
            text = pat.sub(good, text)
    return text


def detect_variant(text: str) -> str:
    if not _T2S:
        return "hant"
    sample = "".join(ch for ch in (text or "") if "\u4e00" <= ch <= "\u9fff")[:400]
    if not sample:
        return "hant"
    changed = sum(1 for a, b in zip(sample, _T2S.convert(sample)) if a != b)
    return "hant" if changed / len(sample) > 0.05 else "hans"


def to_traditional(text: str, variant: str = "") -> str:
    if not text or not _S2TWP:
        return text
    if (variant or detect_variant(text)) == "hant":
        return repair_doubled_phrases(text)
    return repair_doubled_phrases(_S2TWP.convert(text))


def normalize_traditional(text: str) -> str:
    if not text or not _S2TWP:
        return text
    return repair_doubled_phrases(_S2TWP.convert(text))


_ZH_VARIANT_MAP = str.maketrans({"臺": "台", "着": "著", "裏": "裡"})


def fold_zh_variants(text: str) -> str:
    return text.translate(_ZH_VARIANT_MAP) if text else text


def normalize_key_text(text: str) -> str:
    return fold_zh_variants(normalize_traditional(text))


_ENDPOINT = "https://translate.googleapis.com/translate_a/single"
# The text travels in the query string, so the limit that actually matters is
# the length of the *percent-encoded* q, not the character count. One CJK
# character costs 9 bytes encoded, so a 1500-character Japanese chunk becomes a
# ~13,000-byte URL and the endpoint rejects it. Counting characters therefore
# passes for English and fails for every non-Latin language -- which is exactly
# the shape of a backfill that reports failed=988, translated=0.
# Measure the real ceiling with `translate_probe.py --find-limit`; this value is
# deliberately below the lowest figure that probe has ever returned.
_CHUNK_ENCODED_BYTES = 2000
_DEFAULT_TIMEOUT = 30
GTX_SLEEP_BETWEEN_CALLS = 1.0

# Sentence ends, with the whitespace *optional*: CJK and Thai put no space
# after 。！？, so a `(?<=[.!?。！？])\s+` split finds no boundary at all in
# Japanese prose and hands the whole summary over as a single chunk.
_SENT_SPLIT = re.compile(r"(?<=[.!?。！？])\s*")

# Why the last translate() returned None. translate() keeps its Optional
# contract for existing callers, but a failure that leaves no trace is how a
# whole run of these becomes indistinguishable from having nothing to translate
# (ARCHITECTURE 1.8.1). Callers that care read translate_detailed().
LAST_ERROR: str = ""
LAST_PROVIDER: str = ""

DEEPL_KEY = os.environ.get("DEEPL_API_KEY", "").strip()
DEEPL_FREE_HOST = "https://api-free.deepl.com"
DEEPL_PRO_HOST = "https://api.deepl.com"
DEEPL_BATCH = 50
DEEPL_MAX_CHARS = 100_000
_DEEPL_TARGET = {"zh-tw": "ZH-HANT", "zh-hant": "ZH-HANT",
                 "zh-cn": "ZH-HANS", "zh-hans": "ZH-HANS"}


def deepl_enabled() -> bool:
    return bool(DEEPL_KEY) and HAS_REQUESTS


def _deepl_host() -> str:
    return DEEPL_FREE_HOST if DEEPL_KEY.endswith(":fx") else DEEPL_PRO_HOST


def short_reason(reason: str, limit: int = 160) -> str:
    reason = " ".join((reason or "").split())
    for key in ("too many 429", "429", "403", "timed out", "NameResolution",
                "Connection", "quota", "456"):
        if key in reason:
            head = reason.split(":", 1)[0]
            return (head if key in head else f"{head}: {key}")[:limit]
    return reason[:limit]


class TranslateError(Exception):
    """Raised inside translate_detailed so the reason survives the call."""


def _encoded_len(text: str) -> int:
    return len(quote(text, safe=""))


def chunk_text(text: str, limit: int = _CHUNK_ENCODED_BYTES) -> list[str]:
    """Split into pieces whose encoded size fits the endpoint's URL budget.

    Prefers sentence boundaries; falls back to a hard character split for a
    single sentence that is already over budget, because appending it whole
    would produce a request that can only ever fail.
    """
    out: list[str] = []
    cur = ""
    for sent in (p for p in _SENT_SPLIT.split(text or "") if p and p.strip()):
        cand = f"{cur} {sent}".strip() if cur else sent.strip()
        if _encoded_len(cand) <= limit:
            cur = cand
            continue
        if cur:
            out.append(cur)
            cur = ""
        sent = sent.strip()
        while _encoded_len(sent) > limit:
            # Binary-search the longest prefix that fits, so the cut adapts to
            # how expensive this particular script is to encode.
            lo, hi = 1, len(sent)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if _encoded_len(sent[:mid]) <= limit:
                    lo = mid
                else:
                    hi = mid - 1
            out.append(sent[:lo])
            sent = sent[lo:].lstrip()
        cur = sent
    if cur:
        out.append(cur)
    return out


def _translate_chunk(chunk: str, source: str, target: str,
                     session, timeout: int) -> str:
    chunk = chunk.strip()
    if not chunk:
        return ""
    try:
        r = session.get(
            _ENDPOINT,
            params={"client": "gtx", "sl": source, "tl": target, "dt": "t", "q": chunk},
            timeout=timeout,
        )
    except Exception as e:
        raise TranslateError(f"{type(e).__name__}: {e}") from e
    if r.status_code != 200:
        # The status is the whole diagnosis: 429 means slow down, 403 means this
        # IP is refused and retrying costs the run its time budget for nothing.
        raise TranslateError(f"HTTP {r.status_code}")
    try:
        payload = r.json()
    except Exception as e:
        raise TranslateError(f"bad json: {type(e).__name__}") from e
    segs = payload[0] if isinstance(payload, list) and payload else []
    if not isinstance(segs, list):
        raise TranslateError("unexpected payload shape")
    return "".join(str(s[0]) for s in segs if isinstance(s, list) and s and s[0])


def _deepl_call(texts: list[str], target: str, session, timeout: int) -> list[str]:
    tl = _DEEPL_TARGET.get(target.lower(), "ZH-HANT")
    try:
        r = session.post(
            _deepl_host() + "/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {DEEPL_KEY}",
                     "Content-Type": "application/json"},
            json={"text": texts, "target_lang": tl},
            timeout=timeout,
        )
    except Exception as e:
        raise TranslateError(f"deepl {type(e).__name__}: {e}") from e
    if r.status_code == 456:
        raise TranslateError("deepl quota exceeded (456)")
    if r.status_code != 200:
        raise TranslateError(f"deepl HTTP {r.status_code}")
    try:
        out = [t["text"] for t in r.json()["translations"]]
    except Exception as e:
        raise TranslateError(f"deepl bad payload: {type(e).__name__}") from e
    if len(out) != len(texts):
        raise TranslateError(
            f"deepl returned {len(out)} of {len(texts)} segments")
    return out


def deepl_usage(session=None) -> tuple[int, int] | None:
    if not deepl_enabled():
        return None
    own = session is None
    session = session or requests.Session()
    try:
        r = session.get(_deepl_host() + "/v2/usage",
                        headers={"Authorization": f"DeepL-Auth-Key {DEEPL_KEY}"},
                        timeout=15)
        d = r.json()
        return int(d["character_count"]), int(d["character_limit"])
    except Exception:
        return None
    finally:
        if own:
            session.close()


def translate_many(
    texts: list[str],
    target: str = "zh-TW",
    session: Optional["requests.Session"] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> list[tuple[Optional[str], str]]:
    global LAST_PROVIDER
    results: list[tuple[Optional[str], str]] = [(None, "not attempted")] * len(texts)
    idx = [i for i, t in enumerate(texts) if (t or "").strip()]
    for i in range(len(texts)):
        if i not in set(idx):
            results[i] = (None, "empty input")

    own_session = session is None
    if own_session:
        if not HAS_REQUESTS:
            return [(None, "requests not installed")] * len(texts)
        session = requests.Session()
    try:
        pos = 0
        use_deepl = deepl_enabled()
        while pos < len(idx):
            batch, chars = [], 0
            while pos < len(idx) and len(batch) < DEEPL_BATCH:
                t = texts[idx[pos]]
                if batch and chars + len(t) > DEEPL_MAX_CHARS:
                    break
                batch.append(idx[pos])
                chars += len(t)
                pos += 1
            if use_deepl:
                try:
                    out = _deepl_call([texts[i] for i in batch], target,
                                      session, timeout)
                    for i, o in zip(batch, out):
                        results[i] = ((o.strip() or None),
                                      "" if o.strip() else "empty result")
                    LAST_PROVIDER = "deepl"
                    continue
                except TranslateError as e:
                    reason = str(e)
                    if "quota" in reason or "403" in reason or "401" in reason:
                        use_deepl = False
                    for i in batch:
                        results[i] = (None, reason)
            for i in batch:
                res, why = _gtx_detailed(texts[i], target=target,
                                         session=session, timeout=timeout)
                if res:
                    LAST_PROVIDER = "gtx"
                results[i] = (res, why)
                time.sleep(GTX_SLEEP_BETWEEN_CALLS)
        return results
    finally:
        if own_session:
            session.close()


def translate_detailed(
    text: str,
    target: str = "zh-TW",
    source: str = "auto",
    session: Optional["requests.Session"] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[Optional[str], str]:
    global LAST_PROVIDER
    if deepl_enabled() and (text or "").strip():
        own = session is None
        sess = session or requests.Session()
        try:
            out = _deepl_call([text.strip()], target, sess, timeout)[0].strip()
            if out:
                LAST_PROVIDER = "deepl"
                return out, ""
        except TranslateError:
            pass
        finally:
            if own:
                sess.close()
    result, reason = _gtx_detailed(text, target, source, session, timeout)
    if result:
        LAST_PROVIDER = "gtx"
    return result, reason


def _gtx_detailed(
    text: str,
    target: str = "zh-TW",
    source: str = "auto",
    session: Optional["requests.Session"] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> tuple[Optional[str], str]:
    """Translate via the gtx endpoint, returning (result, reason).

    Same work as translate(), but the failure reason comes back instead of
    being swallowed, so a caller can tell a rate limit from a block from an
    empty input and stop early on the ones that will not improve.
    """
    text = (text or "").strip()
    if not text:
        return None, "empty input"
    if session is None and not HAS_REQUESTS:
        return None, "requests not installed"
    own_session = session is None
    if own_session:
        session = requests.Session()
    try:
        out = [_translate_chunk(c, source, target, session, timeout)
               for c in chunk_text(text)]
        result = "".join(out).strip()
        return (result, "") if result else (None, "empty result")
    except TranslateError as e:
        return None, str(e)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    finally:
        if own_session:
            session.close()


def translate(
    text: str,
    target: str = "zh-TW",
    source: str = "auto",
    session: Optional["requests.Session"] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Optional[str]:
    """Optional-returning wrapper kept for existing callers.

    The reason is not lost, only moved: it is in LAST_ERROR and in the tuple
    from translate_detailed().
    """
    global LAST_ERROR
    result, reason = translate_detailed(text, target, source, session, timeout)
    LAST_ERROR = reason
    return result
