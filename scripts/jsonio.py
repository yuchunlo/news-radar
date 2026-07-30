#!/usr/bin/env python3
"""
jsonio.py — 專案共用的 JSON 序列化

整個專案的 JSON 都是「頂層鍵各一行、items 每個條目各一行」。理由：

  indent=2 讓 archive.json 從 23MB 膨脹到 42MB，一半是空白；完全不縮排會變成
  單行巨檔，git diff 完全無法閱讀。一個條目一行兩邊的好處都有——體積接近
  compact，而改動一個條目在 diff 裡就是改一行。

  更重要的是所有寫入 archive.json 的腳本（update_news / summarize_feed /
  download_sub / entity_graph）必須用同一種格式，否則每支腳本輪流跑就會把整
  個檔案重排一次，每次 commit 都是 16,000 行全變動。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

_COMPACT = (",", ":")


def dumps(payload, items_key: str = "items") -> str:
    """序列化成一個條目一行。payload 是 list 時整份當條目陣列處理。"""
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
    assert json.loads(out) == demo, "round-trip failed"
    assert out.count("\n") == 8, out
    assert json.loads(dumps([{"a": 1}, {"b": 2}])) == [{"a": 1}, {"b": 2}]
    assert json.loads(dumps({"only": 1})) == {"only": 1}
    print("jsonio self-test: ok")
