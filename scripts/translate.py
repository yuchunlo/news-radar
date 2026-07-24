#!/usr/bin/env python3

from __future__ import annotations

import re
from typing import Optional

import requests

try:
    from opencc import OpenCC
    _S2TWP = OpenCC("s2twp")
    _T2S = OpenCC("t2s")
    HAS_OPENCC = True
except Exception:
    _S2TWP = _T2S = None
    HAS_OPENCC = False


def to_traditional(text: str) -> str:
    if not text:
        return text
    return _S2TWP.convert(text) if _S2TWP else text


def detect_variant(text: str) -> str:
    if not _T2S:
        return "hant"
    sample = "".join(ch for ch in (text or "") if "\u4e00" <= ch <= "\u9fff")[:400]
    if not sample:
        return "hant"
    changed = sum(1 for a, b in zip(sample, _T2S.convert(sample)) if a != b)
    return "hant" if changed / len(sample) > 0.05 else "hans"


_ENDPOINT = "https://translate.googleapis.com/translate_a/single"
_CHUNK_LIMIT = 1500
_DEFAULT_TIMEOUT = 30


def _translate_chunk(chunk: str, source: str, target: str,
                     session: requests.Session, timeout: int) -> str:
    chunk = chunk.strip()
    if not chunk:
        return ""
    r = session.get(
        _ENDPOINT,
        params={"client": "gtx", "sl": source, "tl": target, "dt": "t", "q": chunk},
        timeout=timeout,
    )
    r.raise_for_status()
    payload = r.json()
    segs = payload[0] if isinstance(payload, list) and payload else []
    if not isinstance(segs, list):
        return ""
    return "".join(str(s[0]) for s in segs if isinstance(s, list) and s and s[0])


def translate(
    text: str,
    target: str = "zh-TW",
    source: str = "auto",
    session: Optional[requests.Session] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> Optional[str]:
    text = (text or "").strip()
    if not text:
        return None
    own_session = session is None
    if own_session:
        session = requests.Session()
    try:
        out, chunk = [], ""
        for sent in re.split(r"(?<=[.!?。！？])\s+", text):
            if len(chunk) + len(sent) > _CHUNK_LIMIT:
                out.append(_translate_chunk(chunk, source, target, session, timeout))
                chunk = sent
            else:
                chunk = f"{chunk} {sent}".strip()
        if chunk:
            out.append(_translate_chunk(chunk, source, target, session, timeout))
        result = "".join(out).strip()
        return result or None
    except Exception:
        return None
    finally:
        if own_session:
            session.close()
