#!/usr/bin/env python3

from __future__ import annotations

import re
from pathlib import Path
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
_CHUNK_LIMIT = 1500
_DEFAULT_TIMEOUT = 30


def _translate_chunk(chunk: str, source: str, target: str,
                     session, timeout: int) -> str:
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
    if session is None and not HAS_REQUESTS:
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
