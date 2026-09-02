#!/usr/bin/env python3
"""
subtitle_priority.py — Single source of truth for subtitle track priority

The downloader (download_sub.py) and the consumer (summarize_feed.pick_subtitle)
originally each maintained their own tier logic. Both implementations looked
correct, but they produced different results. This is the single authoritative
implementation.

## Why this ordering

The summary is in Chinese. The summarize_feed branch is straightforward: if
detect_lang detects zh-hant / zh-hans, it uses extractive_summary(cjk=True);
all other languages go through an additional translate_to_zhtw() step. The
output of that MT step is exactly what entity_extract receives as input—Google
Translate is unstable with proper nouns.
Entity keys are built from these literal strings, so MT noise can split what
should be the same node into multiple nodes. Therefore, "no MT required" is
the strongest consideration.

The second consideration is manual versus auto, which is language-independent,
so it takes precedence over language. Both extractive_summary and
chunker.semantic_chunk rely on punctuation and sentence boundaries, while
automatic subtitles usually have no punctuation. The old version treated
manual/auto as a tiebreaker *within* the language tier, so "auto + original
language" (0, 1) beat "manual + Chinese" (1, 0)—even when an English video
clearly had creator-provided manual Chinese subtitles, it would download the
English ASR and then machine-translate it, taking both disadvantages.

    rank  track                              cost
    ────────────────────────────────────────────────────────────────────
    0     manual, Chinese                   zero MT, punctuation, full budget
    1     manual, original language         punctuation, one Google MT pass
    2     manual, other language             human translation + one MT pass
    3     auto, original language            actual ASR, no punctuation
    4     auto, Chinese                      ASR + YouTube MT, saves one Google pass
    5     auto, other language               ASR + MT
    6     chained (zh-Hant-xx / zh-Hans-xx)  ASR + two MT passes, also watermarked

When the original language is Chinese, rank 0 and rank 1 naturally overlap;
no special case is needed.
"""

from __future__ import annotations

import sys

# Various forms of Chinese. The old version omitted zh-CN / zh-SG, causing
# manually provided Simplified Chinese subtitles to fall into the "other
# language" tier—OpenCC can convert Simplified Chinese to Traditional Chinese,
# so they should be treated as Chinese.
# yt-dlp treats a replay of a live chat as a "language" in subtitles
# (`live_chat`), but its format is JSON rather than VTT. The old version treated
# it as manually provided subtitles in a third language (rank 2), causing any
# video that had live chat enabled to download it, after which --sub-format vtt
# could not retrieve anything and the entire video was marked as failed.
# In one test run, 53 out of 79 entries failed for this reason. It is not a
# language, so filter it out directly from the candidates.
# Note that normalize() converts underscores to hyphens (to support forms such
# as zh_Hant), so the literal used for comparison must also use a hyphen.
NON_LANG_TRACKS = {"live-chat", "rechat"}

ZH_REGIONS = {"tw", "hk", "cn", "mo", "sg"}
ZH_BASE = {"zh", "zh-hant", "zh-hans", "zh-chs", "zh-cht"}

# rank mapping: (is_manual, language class) → rank.
# Language classes are defined by lang_class().
_RANKS = {
    (True,  "zh"):    0,
    (True,  "orig"):  1,
    (True,  "other"): 2,
    (False, "orig"):  3,
    (False, "zh"):    4,
    (False, "other"): 5,
}
CHAINED_RANK = 6

# The consumer only has the filename; the filename does not record manual/auto
# (`{id}.{orig}.{sub}.vtt`), so it uses this language-only ordering. Chinese is
# ranked before the original language: since the downloader already places
# manual Chinese first, a Chinese file on disk is probably manual, and in any
# case it saves one MT pass.
_LANG_ONLY_RANKS = {"zh": 0, "orig": 1, "other": 2}
LANG_ONLY_CHAINED_RANK = 3


def normalize(lang: str | None) -> str:
    return (lang or "").strip().lower().replace("_", "-")


def is_non_lang(lang: str | None) -> bool:
    """A track that is not a language (e.g. a live-chat replay).
    Such tracks should not be candidates.
    """
    return normalize(lang) in NON_LANG_TRACKS


def is_chained(lang: str | None) -> bool:
    """A variant produced when YouTube machine-translates auto subtitles again,
    such as zh-Hant-en.

    The criterion is "the subtag after zh-hant / zh-hans is not a region
    subtag". The old version used `^zh-(hant|hans)-.+` and treated all matches
    as chained, which incorrectly classified valid locales such as zh-Hant-TW
    as double machine-translated subtitles and pushed them to the last rank.
    """
    l = normalize(lang)
    for base in ("zh-hant-", "zh-hans-"):
        if l.startswith(base):
            return l[len(base):] not in ZH_REGIONS
    return False


def is_zh(lang: str | None) -> bool:
    """Chinese, including regional forms and three-part locales such as
    zh-Hant-TW, but excluding chained tracks.
    """
    l = normalize(lang)
    if not l or is_chained(l):
        return False
    if l in ZH_BASE:
        return True
    parts = l.split("-")
    if parts[0] != "zh":
        return False
    # zh-tw / zh-cn..., as well as zh-hant-tw / zh-hans-cn
    return parts[-1] in ZH_REGIONS or l in ZH_BASE


def lang_class(lang: str | None, orig_lang: str | None) -> str:
    """"chained" / "zh" / "orig" / "other". Chinese takes priority over the
    original language: when both are the same, it is classified as "zh", so
    the difference between (True, "zh") and (True, "orig") in the rank table
    has no effect.
    """
    l = normalize(lang)
    if is_chained(l):
        return "chained"
    if is_zh(l):
        return "zh"
    if l and l == normalize(orig_lang):
        return "orig"
    return "other"


def track_rank(lang: str | None, orig_lang: str | None, is_manual: bool) -> int:
    """Lower numbers have higher priority. Used by the downloader, which knows
    whether a track is manual or automatic.
    """
    cls = lang_class(lang, orig_lang)
    if cls == "chained":
        return CHAINED_RANK
    return _RANKS[(bool(is_manual), cls)]


def file_rank(sub_lang: str | None, orig_lang: str | None) -> int:
    """Lower numbers have higher priority. Used by the consumer, which only
    has the filename and does not know whether the track is manual or
    automatic.
    """
    cls = lang_class(sub_lang, orig_lang)
    if cls == "chained":
        return LANG_ONLY_CHAINED_RANK
    return _LANG_ONLY_RANKS[cls]


def choose_track(manual, auto, orig_lang: str | None):
    """Choose one track from yt-dlp's subtitles / automatic_captions dicts.

    Returns (is_manual, lang). Returns None when both are empty. If ranks are
    equal, manual tracks are preferred first, followed by language-code
    ordering, so results remain stable across runs.
    """
    candidates = [(track_rank(l, orig_lang, True), 0, l, True)
                  for l in (manual or {}) if not is_non_lang(l)]
    candidates += [(track_rank(l, orig_lang, False), 1, l, False)
                   for l in (auto or {}) if not is_non_lang(l)]
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

    # Core change in this revision: manual Chinese should beat auto original
    # language subtitles.
    eq(choose_track({"zh-Hant": {}}, {"en": {}}, "en"), (True, "zh-Hant"),
       "manual zh should beat auto original")
    # Manual original language still beats manually provided third-language
    # subtitles.
    eq(choose_track({"en": {}, "fr": {}}, {}, "en"), (True, "en"),
       "manual original should beat manual third language")
    # When there is no manual track, original-language ASR beats YouTube's
    # machine-translated Chinese subtitles.
    eq(choose_track({}, {"en": {}, "zh-Hant": {}}, "en"), (False, "en"),
       "auto original should beat auto-translated zh")
    # Chained tracks always rank last.
    eq(choose_track({}, {"zh-Hant-en": {}, "de": {}}, "en"), (False, "de"),
       "chained should lose to any plain auto track")
    eq(choose_track({"zh-Hant-en": {}}, {}, "en"), (True, "zh-Hant-en"),
       "chained is still better than nothing")
    eq(choose_track({}, {}, "en"), None, "no tracks at all")
    # When the original language is Chinese: manual beats auto, and falls into
    # rank 0.
    eq(choose_track({"zh-TW": {}}, {"zh-TW": {}}, "zh-TW"), (True, "zh-TW"),
       "manual should beat auto for the same language")
    eq(track_rank("zh-TW", "zh-TW", True), 0, "manual zh original is rank 0")

    # live_chat is not a language: when it is the only track, it is equivalent
    # to having no subtitles.
    eq(choose_track({"live_chat": {}}, {}, None), None,
       "live_chat alone is not a subtitle track")
    eq(choose_track({"live_chat": {}, "zh-TW": {}}, {}, "en"), (True, "zh-TW"),
       "live_chat must not be picked over a real track")
    eq(choose_track({"live_chat": {}}, {"en": {}}, "en"), (False, "en"),
       "a real auto track beats live_chat")
    if not is_non_lang("LIVE_CHAT"):
        failures.append("is_non_lang should be case-insensitive")

    # Simplified Chinese regional codes omitted by the old version.
    for l in ("zh-CN", "zh-SG", "zh-Hans-CN", "zh-MO"):
        if not is_zh(l):
            failures.append(f"{l} should count as Chinese")
    eq(choose_track({"zh-CN": {}}, {"en": {}}, "en"), (True, "zh-CN"),
       "manual zh-CN should beat auto original")

    # Chained misclassification: zh-Hant-TW is a valid locale, not a
    # double machine-translated track.
    if is_chained("zh-Hant-TW"):
        failures.append("zh-Hant-TW is a locale, not a chained track")
    for l in ("zh-Hant-en", "zh-Hans-ja", "zh-Hant-ko"):
        if not is_chained(l):
            failures.append(f"{l} should be detected as chained")
    eq(choose_track({"zh-Hant-TW": {}}, {"en": {}}, "en"), (True, "zh-Hant-TW"),
       "zh-Hant-TW should be treated as plain Chinese")

    # Consumer side (language only): Chinese first, chained last.
    eq(file_rank("zh-Hant", "en"), 0, "file: zh first")
    eq(file_rank("en", "en"), 1, "file: original second")
    eq(file_rank("de", "en"), 2, "file: third language")
    eq(file_rank("zh-Hant-en", "en"), 3, "file: chained last")

    # Case and underscores should not affect the classification.
    eq(track_rank("ZH_HANT", "en", True), 0, "case/underscore insensitive")

    # Completeness of the rank table.
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
