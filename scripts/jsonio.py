#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

_COMPACT = (",", ":")


def dumps(payload, items_key: str = "items") -> str:
    if isinstance(payload, list):
        items, head = payload, None
    elif isinstance(payload, dict) and isinstance(payload.get(items_key), list):
        items = payload[items_key]
        head = {k: v for k, v in payload.items() if k != items_key}
    else:
        return json.dumps(payload, ensure_ascii=False, separators=_COMPACT) + "\n"

    lines = []
    if head is None:
        lines.append("[")
    else:
        lines.append("{")
        for key, value in head.items():
            lines.append(f" {json.dumps(key, ensure_ascii=False)}: "
                         f"{json.dumps(value, ensure_ascii=False, separators=_COMPACT)},")
        lines.append(f' {json.dumps(items_key, ensure_ascii=False)}: [')
    last = len(items) - 1
    prefix = " " if head is None else "  "
    for i, item in enumerate(items):
        line = json.dumps(item, ensure_ascii=False, separators=_COMPACT)
        lines.append(prefix + line + ("" if i == last else ","))
    if head is None:
        lines.append("]")
    else:
        lines.append(" ]")
        lines.append("}")
    return "\n".join(lines) + "\n"


def write_atomic(path, payload, items_key: str = "items") -> None:
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
    assert json.loads(out) == demo, "round-trip failed"
    assert out.count("\n") == 8, out
    assert json.loads(dumps([{"a": 1}, {"b": 2}])) == [{"a": 1}, {"b": 2}]
    assert json.loads(dumps({"only": 1})) == {"only": 1}
    print("jsonio self-test: ok")
