#!/usr/bin/env python3
"""
subtitle_priority.py — 字幕軌優先順序的單一判準

下載端（download_sub.py）與取用端（summarize_feed.pick_subtitle）原本各自維護
一份 tier 邏輯，兩份都寫得像對的，但排出來的結果不同。這裡是唯一的那一份。

## 為什麼這樣排

摘要是中文。summarize_feed 的分支很直接：detect_lang 判到 zh-hant / zh-hans
就走 extractive_summary(cjk=True)，其他語言全都多一趟 translate_to_zhtw()。而
那趟 MT 的輸出正是 entity_extract 的輸入——Google Translate 對專有名詞不穩定
（輝達／英偉達／NVIDIA 混用），entity key 建在這些字面上，MT 雜訊會把同一個
節點拆散。所以「不需要 MT」是最強的一項考量。

第二項是 manual 對 auto，而它跟語言無關，所以壓在語言之上：extractive_summary
與 chunker.semantic_chunk 都靠標點與句界工作，自動字幕通常沒有標點。舊版把
manual/auto 放在語言 tier *內部*當 tiebreak，於是「auto + 原語言」(0, 1) 贏過
「manual + 中文」(1, 0)——一支英文影片明明有創作者附的人工中文字幕，卻去下載
英文 ASR 再機器翻譯，兩邊的缺點都吃到了。

    rank  軌道                              代價
    ────────────────────────────────────────────────────────────────────
    0     manual，中文                      零 MT、有標點、全額 budget
    1     manual，原語言                    有標點，一趟 Google MT
    2     manual，其他語言                  人工翻譯 + 一趟 MT
    3     auto，原語言                      真 ASR，無標點
    4     auto，中文                        ASR + YouTube MT，省下 Google 那趟
    5     auto，其他語言                    ASR + MT
    6     chained（zh-Hant-xx / zh-Hans-xx）ASR + 兩趟 MT，還帶浮水印

原語言就是中文時 rank 0 與 1 自然重合，不需要特例。
"""

from __future__ import annotations

import sys

# 中文的各種寫法。zh-CN / zh-SG 舊版漏掉，於是人工簡中字幕掉到「其他語言」那
# 一層——opencc 本來就會把簡中轉繁，它屬於中文。
ZH_REGIONS = {"tw", "hk", "cn", "mo", "sg"}
ZH_BASE = {"zh", "zh-hant", "zh-hans", "zh-chs", "zh-cht"}

# rank 表：(is_manual, 語言類別) → rank。語言類別見 lang_class()。
_RANKS = {
    (True,  "zh"):    0,
    (True,  "orig"):  1,
    (True,  "other"): 2,
    (False, "orig"):  3,
    (False, "zh"):    4,
    (False, "other"): 5,
}
CHAINED_RANK = 6

# 取用端只有檔名，檔名沒有記 manual/auto（`{id}.{orig}.{sub}.vtt`），所以那邊
# 用這份只看語言的順序。中文排在原語言之前：既然下載端已經把 manual 中文排到
# 最前面，磁碟上的中文檔大概就是人工的，而且無論如何都省下一趟 MT。
_LANG_ONLY_RANKS = {"zh": 0, "orig": 1, "other": 2}
LANG_ONLY_CHAINED_RANK = 3


def normalize(lang: str | None) -> str:
    return (lang or "").strip().lower().replace("_", "-")


def is_chained(lang: str | None) -> bool:
    """YouTube 把自動字幕再機翻一次所產生的變體，例如 zh-Hant-en。

    判準是「zh-hant / zh-hans 後面接的不是地區子標籤」。舊版用
    `^zh-(hant|hans)-.+` 一律當 chain，會把 zh-Hant-TW 這種正規 locale 誤判成
    雙重機翻，直接踢到最後一名。
    """
    l = normalize(lang)
    for base in ("zh-hant-", "zh-hans-"):
        if l.startswith(base):
            return l[len(base):] not in ZH_REGIONS
    return False


def is_zh(lang: str | None) -> bool:
    """中文（含各地區寫法與 zh-Hant-TW 這類三段式 locale），但不含 chained。"""
    l = normalize(lang)
    if not l or is_chained(l):
        return False
    if l in ZH_BASE:
        return True
    parts = l.split("-")
    if parts[0] != "zh":
        return False
    # zh-tw / zh-cn…，以及 zh-hant-tw / zh-hans-cn
    return parts[-1] in ZH_REGIONS or l in ZH_BASE


def lang_class(lang: str | None, orig_lang: str | None) -> str:
    """"chained" / "zh" / "orig" / "other"。中文優先於原語言：兩者相同時走
    "zh"，rank 表裡 (True, "zh") 與 (True, "orig") 的差別因此不會有影響。"""
    l = normalize(lang)
    if is_chained(l):
        return "chained"
    if is_zh(l):
        return "zh"
    if l and l == normalize(orig_lang):
        return "orig"
    return "other"


def track_rank(lang: str | None, orig_lang: str | None, is_manual: bool) -> int:
    """數字小的優先。給下載端用（那邊知道軌道是人工還是自動）。"""
    cls = lang_class(lang, orig_lang)
    if cls == "chained":
        return CHAINED_RANK
    return _RANKS[(bool(is_manual), cls)]


def file_rank(sub_lang: str | None, orig_lang: str | None) -> int:
    """數字小的優先。給取用端用（只有檔名，不知道 manual/auto）。"""
    cls = lang_class(sub_lang, orig_lang)
    if cls == "chained":
        return LANG_ONLY_CHAINED_RANK
    return _LANG_ONLY_RANKS[cls]


def choose_track(manual, auto, orig_lang: str | None):
    """從 yt-dlp 的 subtitles / automatic_captions 兩份 dict 挑一軌。

    回傳 (is_manual, lang)，兩邊都空時回傳 None。同 rank 時先人工、再按語言碼
    排序，讓跨輪的結果穩定。
    """
    candidates = [(track_rank(l, orig_lang, True), 0, l, True) for l in (manual or {})]
    candidates += [(track_rank(l, orig_lang, False), 1, l, False) for l in (auto or {})]
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    _, _, lang, is_manual = candidates[0]
    return is_manual, lang


# ─── self-test ───────────────────────────────────────────────────────────────

def _self_test() -> int:
    failures = []

    def eq(got, want, label):
        if got != want:
            failures.append(f"{label}: got {got!r}, want {want!r}")

    # 這次改動的核心：人工中文要贏過原語言的自動字幕
    eq(choose_track({"zh-Hant": {}}, {"en": {}}, "en"), (True, "zh-Hant"),
       "manual zh should beat auto original")
    # 人工原語言仍然贏過人工的第三語言
    eq(choose_track({"en": {}, "fr": {}}, {}, "en"), (True, "en"),
       "manual original should beat manual third language")
    # 沒有人工軌時，原語言的 ASR 勝過 YouTube 機翻的中文
    eq(choose_track({}, {"en": {}, "zh-Hant": {}}, "en"), (False, "en"),
       "auto original should beat auto-translated zh")
    # chained 永遠最後
    eq(choose_track({}, {"zh-Hant-en": {}, "de": {}}, "en"), (False, "de"),
       "chained should lose to any plain auto track")
    eq(choose_track({"zh-Hant-en": {}}, {}, "en"), (True, "zh-Hant-en"),
       "chained is still better than nothing")
    eq(choose_track({}, {}, "en"), None, "no tracks at all")
    # 原語言就是中文：人工勝自動，且落在 rank 0
    eq(choose_track({"zh-TW": {}}, {"zh-TW": {}}, "zh-TW"), (True, "zh-TW"),
       "manual should beat auto for the same language")
    eq(track_rank("zh-TW", "zh-TW", True), 0, "manual zh original is rank 0")

    # 舊版漏掉的簡中地區碼
    for l in ("zh-CN", "zh-SG", "zh-Hans-CN", "zh-MO"):
        if not is_zh(l):
            failures.append(f"{l} should count as Chinese")
    eq(choose_track({"zh-CN": {}}, {"en": {}}, "en"), (True, "zh-CN"),
       "manual zh-CN should beat auto original")

    # chained 的誤判：zh-Hant-TW 是正規 locale，不是雙重機翻
    if is_chained("zh-Hant-TW"):
        failures.append("zh-Hant-TW is a locale, not a chained track")
    for l in ("zh-Hant-en", "zh-Hans-ja", "zh-Hant-ko"):
        if not is_chained(l):
            failures.append(f"{l} should be detected as chained")
    eq(choose_track({"zh-Hant-TW": {}}, {"en": {}}, "en"), (True, "zh-Hant-TW"),
       "zh-Hant-TW should be treated as plain Chinese")

    # 取用端（只看語言）：中文優先，chained 最後
    eq(file_rank("zh-Hant", "en"), 0, "file: zh first")
    eq(file_rank("en", "en"), 1, "file: original second")
    eq(file_rank("de", "en"), 2, "file: third language")
    eq(file_rank("zh-Hant-en", "en"), 3, "file: chained last")

    # 大小寫與底線不該影響判斷
    eq(track_rank("ZH_HANT", "en", True), 0, "case/underscore insensitive")

    # rank 表的完整性
    for manual in (True, False):
        for cls in ("zh", "orig", "other"):
            if (manual, cls) not in _RANKS:
                failures.append(f"missing rank for {(manual, cls)}")
    if len(set(_RANKS.values())) != len(_RANKS):
        failures.append("ranks must be distinct")
    if max(_RANKS.values()) >= CHAINED_RANK:
        failures.append("chained must rank worse than every plain track")

    for f in failures:
        print("FAIL:", f)
    print("subtitle_priority self-test:", "FAILED" if failures else "ok")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_self_test())
