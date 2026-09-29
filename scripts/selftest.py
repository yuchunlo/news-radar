#!/usr/bin/env python3
"""Offline contract checks; CI runs this before touching any data. Each check
guards a failure that actually happened (see ARCHITECTURE.md)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("TRANSLATE", "off")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import common, lang, update_news as un, subtitle_priority as sp, thumbs, textproc, extract, download_sub as ds  # noqa: E402

FAILS: list[str] = []


def eq(got, want, label):
    if got != want:
        FAILS.append(f"{label}: got {got!r}, want {want!r}")


def check_jsonio():
    doc = {"generated_at": "x", "total_items": 2, "items": [{"id": "a"}, {"id": "b", "s": "中"}]}
    out = common.dumps(doc)
    eq(json.loads(out), doc, "round-trip")
    eq(out.count("\n"), 8, "one line per item")
    eq(json.loads(common.dumps([{"a": 1}])), [{"a": 1}], "list payload")
    eq(common.dumps({"items": []}), '{\n "items": [\n ]\n}\n', "empty items")
    eq(common.dumps({"g": 1, "items": [{"a": 1}, {"b": 2}]}),
       '{\n "g": 1,\n "items": [\n  {"a":1},\n  {"b":2}\n ]\n}\n', "exact format")


def check_subtitle_priority():
    c = sp.choose_track
    eq(c({"zh-Hant": {}}, {"en": {}}, "en"), (True, "zh-Hant"), "manual zh beats auto orig")
    eq(c({"en": {}, "fr": {}}, {}, "en"), (True, "en"), "manual orig beats manual other")
    eq(c({}, {"en": {}, "zh-Hant": {}}, "en"), (False, "en"), "auto orig beats auto zh")
    eq(c({}, {"zh-Hant-en": {}, "de": {}}, "en"), (False, "de"), "chained last")
    eq(c({"live_chat": {}}, {}, None), None, "live_chat is not a track")
    eq(c({"live_chat": {}}, {"en": {}}, "en"), (False, "en"), "real track beats live_chat")
    eq(c({"zh-Hant-TW": {}}, {"en": {}}, "en"), (True, "zh-Hant-TW"), "zh-Hant-TW is a locale")
    eq(c({"zh-TW": {}}, {"zh-TW": {}}, "zh-TW"), (True, "zh-TW"), "manual over auto")
    for l in ("zh-CN", "zh-SG", "zh-Hans-CN", "zh-MO"):
        eq(sp.is_zh(l), True, f"{l} is Chinese")
    eq([sp.file_rank(l, "en") for l in ("zh-Hant", "en", "de", "zh-Hant-en")], [0, 1, 2, 3], "file ranks")
    eq(sp.track_rank("ZH_HANT", "en", True), 0, "case/underscore")


def check_vtt_and_process():
    eq(ds.ts(3661.007), "01:01:01.007", "timestamp")
    eq(ds.ts(-3), "00:00:00.000", "negative clamps")
    v = ds.to_vtt([(0.0, 5.0, "a"), (2.0, 1.0, "b"), (9.0, 9.0, "  ")])
    eq(v.startswith("WEBVTT\n\n"), True, "vtt header")
    eq("00:00:05.000 --> 00:00:05.500\nb" in v and v.count("-->") == 2, True, "cue clamping")
    eq(ds.to_vtt([]), "", "no segments")
    eq(ds.run(["sh", "-c", "echo hi; echo boo >&2"], 30), (0, "hi\n", "boo\n"), "run ok")
    marker = f"selftest-{os.getpid()}"
    t0 = time.monotonic()
    rc, _, _ = ds.run(["sh", "-c", f"sh -c 'sleep 30; : {marker}' & sleep 30"], 1)
    eq((rc, time.monotonic() - t0 < 15), (None, True), "timeout returns promptly")
    time.sleep(0.3)
    alive = [p for p in Path("/proc").glob("[0-9]*")
             if marker in (p / "cmdline").read_bytes().decode(errors="replace")] \
        if Path("/proc").is_dir() else []
    eq(alive, [], "process group killed")
    with tempfile.TemporaryDirectory() as d:
        for n in ("abc.en.zh.vtt", "abc.en.zh.vtt.part", "other.en.en.vtt"):
            (Path(d) / n).write_text("WEBVTT")
        eq(sorted(ds.discard(Path(d), "abc")), ["abc.en.zh.vtt", "abc.en.zh.vtt.part"], "discard partial")
        (Path(d) / "keep.en.en.vtt").write_text("WEBVTT")
        eq((ds.remove_orphans(Path(d), set()), ds.remove_orphans(Path(d), {"keep"})), (0, 1),
           "orphans removed; an empty archive removes nothing")
        eq(sorted(p.name for p in Path(d).iterdir()), ["keep.en.en.vtt"], "only orphans removed")
    import importlib.util, types
    real = importlib.util.find_spec
    importlib.util.find_spec = lambda name: object()
    try:
        a = types.SimpleNamespace(no_transcribe=False, max_transcribe=0, max_asr_duration=3600,
                                  max_asr_total=7200)
        b = ds.Budget(a)
        eq(b.refuse(4000, False)[1], True, "too long for ASR is permanent")
        eq(b.refuse(100, True)[1], False, "live is not permanent")
        b.spent = 7000
        eq((b.refuse(1000, False)[1], b.stop()), (False, True), "budget wall is not permanent; stops run")
    finally:
        importlib.util.find_spec = real


def check_language():
    nt = lang.needs_translation
    eq(nt("這是一篇講 Python 的文章，使用 requests 與 asyncio 以及 FastAPI framework and uvicorn"),
       False, "繁中 about code is not foreign")
    eq(nt("This is an English summary with more than eight Latin words in it."), True, "English")
    eq(nt("今日は東京で新しいカフェがオープンしました。とても人気があって、たくさんの人が並んでいます。"),
       True, "Japanese despite kanji")
    eq(nt("오늘 서울에서 새로운 카페가 문을 열었습니다 많은 사람들이 줄을 섰습니다"), True, "Korean")
    eq(nt(common.GONE_SUMMARY), False, "placeholder")
    eq(nt("這篇 about using the requests library with asyncio and FastAPI to build servers quickly"),
       False, "a little Han means Chinese with code")
    eq((common.is_pending({"url": "u"}), common.is_pending({"url": "u", "summary": " "})),
       (True, False), "blank summary is not pending")
    eq(nt(common.BLANK_SUMMARY), False, "blank")
    ja = "今日は東京で新しいカフェがオープンしました。" * 200
    chunks = lang.chunk_text(ja)
    eq(len(chunks) > 1 and all(lang._enc(c) <= lang.GTX_CHUNK_BYTES for c in chunks), True,
       "chunks bounded by encoded bytes and split without spaces")
    eq(lang.short_reason("RetryError: " + "x" * 5000 + " too many 429 error responses")[:40],
       "RetryError: too many 429", "reason on one line")


class FakeResp:
    def __init__(self, code, payload):
        self.status_code, self._p = code, payload

    def json(self):
        return self._p


class FakeSession:
    def __init__(self, deepl_code=200, gtx_code=200):
        self.deepl_code, self.gtx_code, self.posts, self.gets = deepl_code, gtx_code, [], 0

    def post(self, url, json=None, **_):
        self.posts.append(json)
        if self.deepl_code == 413 and len(json["text"]) > 1:
            return FakeResp(413, None)
        if self.deepl_code == "short":
            return FakeResp(200, {"translations": [{"text": "譯"}]})
        return FakeResp(200 if self.deepl_code == 413 else self.deepl_code,
                        {"translations": [{"text": "譯" + t} for t in json["text"]]})

    def get(self, url, params=None, **_):
        self.gets += 1
        return FakeResp(self.gtx_code, [[["譯" + params["q"], params["q"]]]])


def check_translate():
    lang.GTX_SLEEP = 0
    saved = lang.deepl_on
    try:
        lang.deepl_on = True
        s = FakeSession()
        out = lang.translate_many(["a", "", "b"], s)
        eq(out, [("譯a", ""), (None, "empty input"), ("譯b", "")], "deepl keeps order and alignment")
        eq(s.posts[0]["target_lang"], "ZH-HANT", "zh-TW maps to ZH-HANT")
        s = FakeSession(deepl_code=413)
        eq(([o for o, _ in lang.translate_many(["a", "b", "c"], s)], s.gets), (["譯a", "譯b", "譯c"], 0),
           "413 halves the batch and stays on DeepL")
        s = FakeSession(deepl_code="short")
        eq((lang.translate_many(["a", "b"], s)[1][0], s.gets), ("譯b", 2),
           "misaligned DeepL result rejected, gtx used instead")
        s = FakeSession(deepl_code=456, gtx_code=429)
        out = lang.translate_many(list("abcdefgh"), s, stop_after=4)
        eq((lang.deepl_on, s.gets, out[-1][1].startswith("skipped")), (False, 4, True),
           "quota disables DeepL; gtx stops at the Nth failure")
    finally:
        lang.deepl_on = saved


def check_thumbs():
    eq([u for u in thumbs.DENY if u != u.lower()], [], "deny entries are lower-case")
    ok = lambda u: thumbs.usable(u)[0]
    eq(ok("https://ritholtz.com/wp-content/uploads/2025/05/MIB_2025.png?w=300"), False, "deny ignores case/query")
    eq(ok("https://blogger.googleusercontent.com/img/a/AVvXsEabc"), True, "trusted host")
    eq(ok("https://example.com/img/us-inflation-chart-1024x576.png"), True, "chart")
    eq(ok("https://example.com/img/us-inflation-rate-chart-since-1970.png"), True,
       "chart vocabulary beats the descriptive-phrase rule")
    eq(ok("https://img.youtube.com/vi/x/mqdefault.jpg"), True, "mqdefault is not 'default'")
    for bad in ("og-image.png", "logo.png", "line@2x.png", "man-standing-near-building.jpg",
                "shutterstock_123.jpg", "spinner.gif"):
        eq(ok("https://example.com/img/" + bad), False, f"reject {bad}")


def check_text():
    html = ("<meta content='width=1100' name='viewport'/>"
            "<meta content='the real one' name='description'/>")
    eq(extract.meta_description(html), "the real one", "meta attrs never cross tags")
    eq(extract.meta_description('<meta name="description" content="plain">'
                                '<meta property="og:description" content="og">'), "og",
       "og:description preferred")
    eq(extract.meta_description('<meta name="description" content="first">'
                                '<meta name="description" content="later">'), "first",
       "first tag of a key wins")
    eq(extract.is_junk("豆瓣 载入中"), True, "junk in simplified")
    text = "第一句話在這裡。第二句話也在這裡。第三句。"
    eq(textproc.trim_to_sentences(text, 12), "第一句話在這裡。", "whole sentences only")
    eq(textproc.trim_to_sentences(text, 3), "第一句話在這裡。", "first sentence kept whole")
    vtt = ("WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.000\nhello everyone and welcome\n\n"
           "2\n00:00:02.000 --> 00:00:04.000\nhello everyone and welcome\nto the gadgets that I use\n")
    with tempfile.NamedTemporaryFile("w", suffix=".vtt", delete=False) as f:
        f.write(vtt)
    eq(textproc.vtt_to_text(f.name), "hello everyone and welcome\nto the gadgets that I use",
       "rolling cues stitched, cue numbers dropped")
    os.unlink(f.name)
    eq(textproc.stitch([["my favorite low-tech"], ["Previously, you guys liked"]]),
       ["my favorite low-tech", "Previously, you guys liked"], "wrapped cues kept whole")
    eq(lang.to_twp("软件开发"), "軟體開發", "s2twp")
    eq(textproc.merge_caption_lines("新しいカフェが\nオープンしたので\n行ってみたいと思います\n"
                                    "でも人がすごく多くて\n並ぶのに一時間かかりました", "ja"),
       "新しいカフェがオープンしたので、行ってみたいと思います。\n"
       "でも人がすごく多くて、並ぶのに一時間かかりました。", "Japanese caption regrouping")


def check_bestblogs():
    class R:
        status_code = 200
        def __init__(self, text): self.text = text
    page = lambda body, meta="": ('<html><head><meta property="og:title" content="Issue | BestBlogs.dev">'
                                  f'{meta}</head><body><main>{body}{"x" * 500}</main></body></html>')
    real = un.session
    try:
        un.session = lambda: type("S", (), {"get": lambda self, *a, **k: R(page("Newsletter2024-06-12 "))})()
        title, body, when = un.bestblogs_issue(4, None)
        eq((title, un.iso(when), bool(body)), ("Issue", "2024-06-12T00:00:00Z", True), "date from page")
        un.session = lambda: type("S", (), {"get": lambda self, *a, **k: R(page(
            "", '<meta property="article:published_time" content="01-05">'))})()
        eq(un.iso(un.bestblogs_issue(9, un.parse_date("2025-12-28"))[2]), "2026-01-05T00:00:00Z",
           "MM-DD rolls into next year")
        eq(un.iso(un.bestblogs_issue(9, un.parse_date("2026-02-10"))[2]), "2026-01-05T00:00:00Z",
           "out-of-order date within a year does not roll over")
    finally:
        un.session = real
    keep, other = {"id": "a", "last_seen_at": "2026-01-01"}, {"id": "b", "last_seen_at": "2026-02-01", "x": 1}
    un.absorb(keep, other)
    eq(keep, {"id": "a", "last_seen_at": "2026-02-01", "x": 1}, "absorb")
    keep = {"x": 1, "summary": "long fallback text ↛"}
    un.absorb(keep, {"x": 2, "summary": "real"})
    eq(keep, {"x": 1, "summary": "real"}, "known values kept; real summary beats ↛")
    # id formula must never change: ids name subtitle files
    eq(un.make_id("tech", "BestBlogs", "BestBlogs.dev 每周精选 第2期",
                  "https://www.bestblogs.dev/newsletter/issue2"),
       "2ee116b6576fe05bdb7123a14831d5d79e32a867", "id formula unchanged (real record)")
    eq(un.make_id("Tech", "X", "T", "https://a.com"), un.make_id("tech", "x", "t", "https://a.com"),
       "id is case-insensitive")
    eq(un.canonical_url("https://vocus.cc/@who/abc123?utm_source=x"), "https://vocus.cc/article/abc123",
       "vocus canonical + tracking stripped")
    now = un.parse_date("2026-09-29")
    arch = {}
    raw = un.Raw("tech", "S", "T", "https://a.com/p", un.parse_date("2026-09-01"), content="body")
    un.ingest(arch, [raw], now)
    rec = next(iter(arch.values()))
    eq(list(rec), ["id", "category", "source", "title", "url", "published_at", "last_seen_at",
                   "feed_content"], "new record fields (no site_name / first_seen_at)")
    import dataclasses
    un.ingest(arch, [dataclasses.replace(raw, published_at=un.parse_date("2026-09-02"))], now)
    eq(rec["published_at"], "2026-09-02T00:00:00Z", "feed date overwrites any category")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("check_"):
            fn()
    for f in FAILS:
        print("FAIL:", f)
    print("selftest:", "FAILED" if FAILS else "ok")
    sys.exit(1 if FAILS else 0)
