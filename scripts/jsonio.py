#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

_COMPACT = (",", ":")
_META_KEY = "__meta__"


def dumps(payload, items_key: str = "items") -> str:
    if isinstance(payload, list):
        items, head = payload, None
    elif isinstance(payload, dict) and isinstance(payload.get(items_key), list):
        items = payload[items_key]
        head = {k: v for k, v in payload.items() if k != items_key}
    else:
        return json.dumps(payload, ensure_ascii=False, separators=_COMPACT) + "\n"

    lines = []
    if head is not None:
        meta = {_META_KEY: True, **head}
        lines.append(json.dumps(meta, ensure_ascii=False, separators=_COMPACT))
    for item in items:
        lines.append(json.dumps(item, ensure_ascii=False, separators=_COMPACT))
    return "\n".join(lines) + ("\n" if lines else "")


def loads(text: str, items_key: str = "items"):
    items: list = []
    head: dict | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        value = json.loads(line)
        if (head is None and not items
                and isinstance(value, dict) and value.get(_META_KEY)):
            head = {k: v for k, v in value.items() if k != _META_KEY}
            continue
        items.append(value)
    if head is not None:
        return {**head, items_key: items}
    return items


def load(path, items_key: str = "items"):
    with open(path, "r", encoding="utf-8") as f:
        return loads(f.read(), items_key)


def write_atomic(path, payload, items_key: str = "items") -> None:
    """同目錄 tempfile + os.replace，避免寫入中斷留下半個檔案。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = dumps(payload, items_key)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


if __name__ == "__main__":
    demo = {"generated_at": "x", "total_items": 2,
            "items": [{"id": "a", "n": 1}, {"id": "b", "n": 2}]}
    out = dumps(demo)
    lines = out.splitlines()
    assert len(lines) == 3, lines            # meta line + 2 items, no wrapping
    assert all(json.loads(ln) for ln in lines), "every line must parse alone"
    assert json.loads(lines[0]) == {"__meta__": True, "generated_at": "x",
                                     "total_items": 2}
    assert loads(out) == demo, "round-trip failed"

    plain = [{"a": 1}, {"b": 2}]
    out2 = dumps(plain)
    assert len(out2.splitlines()) == 2
    assert loads(out2) == plain

    assert loads(dumps([])) == []
    assert loads(dumps({"only": 1, "items": []})) == {"only": 1, "items": []}
    print("jsonio self-test: ok")
