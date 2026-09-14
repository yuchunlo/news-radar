#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jsonio
import subtitle_priority

# Same sentinel summarize_feed uses: a deliberate "this item correctly has
# no summary", as opposed to a missing key, which means "not processed yet".
BLANK_SUMMARY = " "
import local_transcribe
from subtitle_priority import choose_track

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

PROBE_TIMEOUT = 90
DOWNLOAD_TIMEOUT = 240
KILL_GRACE = 5

YTDLP_COMMON_ARGS = [
    "--user-agent", USER_AGENT,
    "--extractor-args", "youtube:player_client=web",
    "--remote-components", "ejs:github",
    "--ignore-no-formats",
    "--no-progress",
    "--socket-timeout", "30",
]


def _kill_tree(proc: subprocess.Popen) -> None:
    if not hasattr(os, "killpg"):
        proc.kill()
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        try:
            proc.wait(timeout=KILL_GRACE)
        except subprocess.TimeoutExpired:
            pass
        if sig is signal.SIGTERM:
            time.sleep(0.2)


def run_ytdlp(cmd: list[str], timeout: float):
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", err or "", False
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=KILL_GRACE)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return None, out or "", err or "", True


def is_youtube(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def already_downloaded(out_dir: Path, item_id: str) -> bool:
    """Fresh check for one id. Only for re-checking right after a download;
    the main loop pre-filters with downloaded_ids() instead."""
    return any(out_dir.glob(f"{item_id}*.vtt"))


def downloaded_ids(out_dir: Path) -> set[str]:
    """Every id that already has a .vtt, from a single directory scan.

    The loop walks the whole archive, so calling already_downloaded() per item
    meant one glob per YouTube entry -- 3,500 directory scans a run on the
    current archive, all of them to re-derive the same listing."""
    return {p.name.split(".", 1)[0] for p in out_dir.glob("*.vtt")}


class CookiesExpired(Exception):
    """Cookies are invalid; nothing further will succeed this run."""


def probe_subtitle_langs(url: str, cookies_path: Path,
                         timeout: float = PROBE_TIMEOUT):
    """Fetch the list of available subtitle languages

    Returns:
      ("EXPIRED", None, None, None, None)  cookies are invalid
      None                                  probe failed (transient; skip)
      (manual, auto, orig_lang, duration, is_live)  normal result

    duration / is_live come from the same --dump-json call and cost nothing
    extra. They let the ASR fallback bail out *before* downloading tens of MB
    of audio for a six-hour livestream replay.
    """
    cmd = [
        "yt-dlp",
        "--cookies", str(cookies_path),
        *YTDLP_COMMON_ARGS,
        "--skip-download",
        "--dump-json",
        url,
    ]
    rc, stdout, stderr, timed_out = run_ytdlp(cmd, timeout)
    output = stdout + stderr
    if "cookies" in output.lower():
        return "EXPIRED", None, None, None, None
    if timed_out:
        return None
    if rc != 0 or not stdout.strip():
        return None
    try:
        # --dump-json prints one JSON object per line; take the last line in
        # case anything else got mixed into stdout.
        info = json.loads(stdout.strip().splitlines()[-1])
    except Exception:
        return None
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    orig_lang = info.get("language")
    duration = info.get("duration") or 0.0
    is_live = bool(info.get("is_live") or info.get("live_status") == "is_live")
    return manual, auto, orig_lang, duration, is_live


def download_one_subtitle(
    url: str, cookies_path: Path, output_tpl: str, lang: str, is_manual: bool,
    timeout: float = DOWNLOAD_TIMEOUT,
):
    cmd = [
        "yt-dlp",
        "--cookies", str(cookies_path),
        *YTDLP_COMMON_ARGS,
        "--skip-download",
        "--write-sub" if is_manual else "--write-auto-sub",
        "--sub-langs", lang,
        "--sub-format", "vtt",
        "--sleep-interval", "4",
        "--max-sleep-interval", "7",
        "--concurrent-fragments", "7",
        "-o", output_tpl,
        url,
    ]
    rc, stdout, stderr, timed_out = run_ytdlp(cmd, timeout)
    return rc, stdout + stderr, timed_out


def discard_partial(out_dir: Path, item_id: str) -> list[str]:
    removed = []
    for pattern in (f"{item_id}*.vtt", f"{item_id}*.vtt.part", f"{item_id}*.part"):
        for path in out_dir.glob(pattern):
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                pass
    return removed


class AsrBudget:
    """Every reason ASR might not run, and the accounting behind them.

    These checks used to be split in two: `available` / `is_live` /
    `max_asr_duration` inside try_transcribe, and `--no-transcribe` /
    `transcribed < max_transcribe` repeated at each of the two call sites in
    the main loop. Same condition written twice is one edit away from the two
    paths disagreeing, so it all lives here now.

    The count cap is also the wrong unit. Twenty videos is a trivial job at 10
    minutes each and an impossible one at 3 hours each, and with the ceiling
    raised to 3 hours the count no longer bounds anything that matters. What
    actually has to fit in the runner is *audio-seconds*, so the budget is
    denominated in those; the count cap stays as a secondary guard.
    """

    def __init__(self, enabled: bool, max_items: int, max_total: float,
                 max_duration: float):
        self.enabled = enabled
        self.max_items = max_items
        self.max_total = max_total
        self.max_duration = max_duration
        self.count = 0
        self.spent = 0.0          # audio-seconds committed this run

    def remaining(self) -> float:
        return self.max_total - self.spent if self.max_total else float("inf")

    def why_not(self, duration: float, is_live: bool) -> tuple[str, bool] | None:
        """(reason, permanent) to skip, or None to go ahead.

        `permanent` decides whether the caller may write the BLANK_SUMMARY
        sentinel. Only one reason here is a property of the video itself --
        being longer than the per-video cap. Every other reason is about this
        run: the budget is spent, the operator turned ASR off, whisper is not
        installed, the stream has not ended yet. Marking those permanently
        would repeat the `blocked` placeholder mistake: the sentinel fills
        `summary`, so the item stops being pending and no later run retries it,
        even though the very next run would have had budget for it.
        """
        if not self.enabled:
            return "transcription disabled", False
        ok, why = local_transcribe.available()
        if not ok:
            return why, False
        if is_live:
            return "still live", False          # it will end
        if self.max_items and self.count >= self.max_items:
            return f"per-run item cap reached ({self.max_items})", False
        if self.max_duration and duration > self.max_duration:
            # A stable fact about this video, not about this run.
            return (f"{duration / 60:.0f} min > "
                    f"{self.max_duration / 60:.0f} min"), True
        if self.max_total and duration > self.remaining():
            return (f"{duration / 60:.0f} min exceeds remaining budget "
                    f"{self.remaining() / 60:.0f} min"), False
        return None

    def record(self, duration: float) -> None:
        self.count += 1
        self.spent += max(duration, 0.0)

    def report(self) -> str:
        return (f"asr={self.count} "
                f"audio={self.spent / 60:.0f}min"
                + (f"/{self.max_total / 60:.0f}min" if self.max_total else ""))


def try_transcribe(url: str, cookies_path: Path, out_dir: Path, item_id: str,
                   orig_lang: str | None, args, budget: AsrBudget,
                   duration: float = 0.0, is_live: bool = False):
    """Returns True on success, or (False, permanent) when ASR did not run.

    `permanent` tells the caller whether this item can be marked as having no
    summary for good, or must be left pending for the next run.
    """
    verdict = budget.why_not(duration, is_live)
    if verdict:
        why, permanent = verdict
        print(f"[ASR-SKIP] item {item_id}: {why}"
              + ("" if permanent else " (kept pending)"))
        return False, permanent
    try:
        path, detected = local_transcribe.transcribe_to_vtt(
            url=url,
            cookies_path=cookies_path,
            out_dir=out_dir,
            item_id=item_id,
            orig_lang=orig_lang,
            common_args=YTDLP_COMMON_ARGS,
            model_name=args.whisper_model,
            compute_type=args.whisper_compute_type,
            audio_timeout=args.audio_timeout,
            max_duration=args.max_asr_duration,
            duration_hint=duration,
            runner=run_ytdlp,
        )
    except local_transcribe.TranscribeUnavailable as exc:
        # Environment problem, not a problem with this video: retry next run.
        discard_partial(out_dir, item_id)
        print(f"[ASR-FAILED] item {item_id}: {exc} (kept pending)")
        return False, False
    except Exception as exc:
        # The video itself could not be transcribed (no audio stream, decode
        # error). Retrying would fail the same way, so let it be marked.
        discard_partial(out_dir, item_id)
        print(f"[ASR-FAILED] item {item_id}: {exc}")
        return False, True
    budget.record(duration)
    print(f"[ASR] item {item_id}: {path.name} (lang={detected}, "
          f"model={args.whisper_model}, {duration / 60:.0f}min, "
          f"{budget.report()})")
    return True, True


def run_asr(item: dict, url: str, item_id: str, orig_lang, args,
            budget: "AsrBudget", duration: float, is_live: bool,
            cookies_path: Path, out_dir: Path, stats: Counter) -> bool:
    """Attempt ASR and record the outcome. Returns whether the item changed.

    Both places that fall back to ASR -- no track offered, and yt-dlp reporting
    no subtitles after the fact -- need exactly this, so it lives here rather
    than being written out twice.
    """
    ok, permanent = try_transcribe(url, cookies_path, out_dir, item_id,
                                   orig_lang, args, budget, duration, is_live)
    if ok:
        stats["succeeded"] += 1
        return False
    if permanent:
        stats["no_subs"] += 1
        item["summary"] = BLANK_SUMMARY
        return True
    stats["deferred"] += 1        # left pending for the next run
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive")
    parser.add_argument("--output-dir")
    parser.add_argument("--cookies-path")
    parser.add_argument("--max-items", type=int, default=70)
    parser.add_argument("--probe-timeout", type=float, default=PROBE_TIMEOUT)
    parser.add_argument("--download-timeout", type=float, default=DOWNLOAD_TIMEOUT)
    parser.add_argument("--no-transcribe", action="store_true",
                        help="Disable local ASR fallback")
    parser.add_argument("--whisper-model", default=local_transcribe.DEFAULT_MODEL)
    parser.add_argument("--whisper-compute-type",
                        default=local_transcribe.DEFAULT_COMPUTE_TYPE)
    parser.add_argument("--audio-timeout", type=float,
                        default=local_transcribe.AUDIO_TIMEOUT)
    parser.add_argument("--max-asr-duration", type=float,
                        default=local_transcribe.MAX_DURATION,
                        help="Skip ASR on videos longer (0 = unlimited)")
    parser.add_argument("--max-transcribe", type=int, default=20,
                        help="Maximum number of videos to transcribe "
                             "(secondary guard; 0 = unlimited)")
    parser.add_argument("--max-asr-total", type=float, default=4 * 60 * 60,
                        metavar="SECONDS",
                        help="Total audio-seconds to transcribe per run "
                             "(0 = unlimited). This is the real cap: the "
                             "runner budget is spent on audio length, not on "
                             "number of videos.")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()
    for required in ("archive", "output_dir", "cookies_path"):
        if not getattr(args, required):
            parser.error(f"--{required.replace('_', '-')} is required")

    archive_path = Path(args.archive)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cookies_path = Path(args.cookies_path)

    data = json.loads(archive_path.read_text(encoding="utf-8"))
    items = data.get("items", [])

    budget = AsrBudget(
        enabled=not args.no_transcribe,
        max_items=args.max_transcribe,
        max_total=args.max_asr_total,
        max_duration=args.max_asr_duration,
    )
    dirty = False
    stats: Counter = Counter()
    # One directory scan up front instead of a glob per item.
    have_subs = downloaded_ids(out_dir)

    def pending(item: dict) -> tuple[str, str] | None:
        """(url, id) for an item still needing subtitles, else None.

        Ordered cheapest test first: a dict lookup rules out the ~3,500
        already-summarised YouTube entries before anything touches the
        filesystem.
        """
        url, item_id = item.get("url", ""), item.get("id")
        if not item_id or item.get("summary") is not None:
            return None
        if not is_youtube(url) or item_id in have_subs:
            return None
        return url, item_id

    try:
        for item in items:
            if stats["processed"] >= args.max_items:
                break
            target = pending(item)
            if target is None:
                continue
            url, item_id = target

            probe = probe_subtitle_langs(url, cookies_path, args.probe_timeout)
            if probe is None:
                stats["failed"] += 1
                stats["processed"] += 1
                print(f"[FAILED] item {item_id}: could not probe subtitle languages")
                continue
            if probe[0] == "EXPIRED":
                raise CookiesExpired
            manual, auto, orig_lang, duration, is_live = probe
            stats["processed"] += 1

            # The probe already paid for this; keeping it means later runs and
            # the summariser can see how long an item is without re-probing.
            # Stored as `length` (whole seconds); `duration` stays the name of
            # yt-dlp's own json key that it is read from.
            if duration and item.get("length") != int(duration):
                item["length"] = int(duration)
                dirty = True

            def asr(item=item, url=url, item_id=item_id, orig_lang=orig_lang,
                    duration=duration, is_live=is_live) -> bool:
                return run_asr(item, url, item_id, orig_lang, args, budget,
                               duration, is_live, cookies_path, out_dir, stats)

            choice = choose_track(manual, auto, orig_lang)
            if choice is None:
                dirty |= asr()
                continue

            is_manual, lang = choice
            print(f"[TRACK] item {item_id}: {lang} "
                  f"({'manual' if is_manual else 'auto'}, orig={orig_lang}, "
                  f"rank={subtitle_priority.track_rank(lang, orig_lang, is_manual)})")
            returncode, output, timed_out = download_one_subtitle(
                url, cookies_path,
                str(out_dir / f"{item_id}.%(language)s.%(ext)s"),
                lang, is_manual, args.download_timeout)
            lowered = output.lower()

            if "cookies" in lowered:
                raise CookiesExpired
            if timed_out:
                removed = discard_partial(out_dir, item_id)
                stats["timed_out"] += 1
                stats["failed"] += 1
                print(f"[TIMEOUT] item {item_id}: timeout over "
                      f"{args.download_timeout}s, Halted"
                      + (f", cleaned {len(removed)} files" if removed else ""))
            elif returncode != 0:
                stats["failed"] += 1
                print(f"[FAILED] item {item_id} (exit {returncode}): {output}")
            elif already_downloaded(out_dir, item_id):
                have_subs.add(item_id)
                stats["succeeded"] += 1
            elif "no subtitles for the requested languages" in lowered:
                dirty |= asr()
            else:
                stats["failed"] += 1
                print(f"[FAILED] item {item_id} (exit 0): {output}")
    except CookiesExpired:
        print("[EXPIRED] cookies invalid")

    if dirty:
        # jsonio, not json.dumps(indent=2): every other script in the pipeline
        # writes this file through jsonio, and rewriting it here with a
        # different indent reformatted the whole archive on any run that
        # touched an item. Also keyed off `dirty` rather than `no_subs`, so a
        # run that only recorded durations still saves them.
        jsonio.write_atomic(archive_path, data)

    print(f"Done. processed={stats['processed']} "
          f"succeeded={stats['succeeded']} ({budget.report()}) "
          f"failed={stats['failed']} no_subs={stats['no_subs']} "
          f"deferred={stats['deferred']} timed_out={stats['timed_out']}")


def _find_processes(marker: str) -> list[int]:
    found = []
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return found
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        if marker in cmdline:
            found.append(int(entry.name))
    return found


def _self_test() -> int:
    import tempfile
    import time

    failures = []

    rc, out, err, timed_out = run_ytdlp(["sh", "-c", "echo hi; echo boo >&2"], 30)
    if timed_out or rc != 0 or out.strip() != "hi" or err.strip() != "boo":
        failures.append(f"normal run: rc={rc} out={out!r} err={err!r} to={timed_out}")

    t0 = time.monotonic()
    rc, out, err, timed_out = run_ytdlp(["sh", "-c", "sleep 30"], 1)
    elapsed = time.monotonic() - t0
    if not timed_out or rc is not None:
        failures.append(f"hang should time out: rc={rc} to={timed_out}")
    if elapsed > 15:
        failures.append(f"timeout took too long to return: {elapsed:.1f}s")

    marker = f"downloadsub-selftest-{os.getpid()}"
    t0 = time.monotonic()
    rc, out, err, timed_out = run_ytdlp(
        ["sh", "-c", f"sh -c 'sleep 30 {marker}' & exit 0"], 2)
    elapsed = time.monotonic() - t0
    if elapsed > 10:
        failures.append(f"orphaned grandchild blocked cleanup: {elapsed:.1f}s")
    time.sleep(0.3)
    survivors = _find_processes(marker)
    if survivors:
        failures.append(f"process group survived the timeout: pids {survivors}")

    with tempfile.TemporaryDirectory() as d:
        out_dir = Path(d)
        for name in ("abc.en.zh-Hant.vtt", "abc.en.zh-Hant.vtt.part", "other.en.en.vtt"):
            (out_dir / name).write_text("WEBVTT", encoding="utf-8")
        removed = discard_partial(out_dir, "abc")
        if already_downloaded(out_dir, "abc"):
            failures.append("partial files should be gone after discard_partial")
        if not already_downloaded(out_dir, "other"):
            failures.append("discard_partial must not touch other items")
        if len(removed) != 2:
            failures.append(f"expected 2 files removed, got {removed}")

    for f in failures:
        print("FAIL:", f)
    print("download_sub self-test:", "FAILED" if failures else "ok")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main() or 0)
