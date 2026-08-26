#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import subtitle_priority
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
    return any(out_dir.glob(f"{item_id}*.vtt"))


def probe_subtitle_langs(url: str, cookies_path: Path,
                         timeout: float = PROBE_TIMEOUT):
    """Fetch the list of available subtitle languages

    Returns:
      ("EXPIRED", None, None)   cookies are invalid
      None                      probe failed (transient; skip for this run)
      (manual, auto, orig_lang) normal result
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
        return "EXPIRED", None, None
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
    return manual, auto, orig_lang


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


def try_transcribe(url: str, cookies_path: Path, out_dir: Path, item_id: str,
                   orig_lang: str | None, args) -> bool:
    ok, why = local_transcribe.available()
    if not ok:
        print(f"[ASR-SKIP] item {item_id}: {why}")
        return False
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
            runner=run_ytdlp,
        )
    except Exception as exc:
        discard_partial(out_dir, item_id)
        print(f"[ASR-FAILED] item {item_id}: {exc}")
        return False
    print(f"[ASR] item {item_id}: {path.name} (lang={detected}, "
          f"model={args.whisper_model})")
    return True


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
                        help="Maximum number of videos to transcribe")
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

    processed = 0
    succeeded = 0
    transcribed = 0
    failed = 0
    timed_out_count = 0
    no_subs = 0

    for item in items:
        if processed >= args.max_items:
            break

        url = item.get("url", "")
        item_id = item.get("id")

        if not item_id or not is_youtube(url):
            continue
        if already_downloaded(out_dir, item_id):
            continue
        if item.get("summary") is not None:
            continue

        probe = probe_subtitle_langs(url, cookies_path, args.probe_timeout)
        if probe is None:
            failed += 1
            print(f"[FAILED] item {item_id}: could not probe subtitle languages")
            processed += 1
            continue
        if probe[0] == "EXPIRED":
            print("[EXPIRED] cookies invalid")
            break

        manual, auto, orig_lang = probe
        choice = choose_track(manual, auto, orig_lang)
        if choice is None:
            if (not args.no_transcribe and transcribed < args.max_transcribe
                    and try_transcribe(url, cookies_path, out_dir, item_id,
                                       orig_lang, args)):
                transcribed += 1
                succeeded += 1
            else:
                no_subs += 1
                item["summary"] = " "
            processed += 1
            continue

        is_manual, lang = choice
        print(f"[TRACK] item {item_id}: {lang} "
              f"({'manual' if is_manual else 'auto'}, orig={orig_lang}, "
              f"rank={subtitle_priority.track_rank(lang, orig_lang, is_manual)})")
        output_tpl = str(out_dir / f"{item_id}.%(language)s.%(ext)s")
        returncode, output, timed_out = download_one_subtitle(
            url, cookies_path, output_tpl, lang, is_manual, args.download_timeout)
        lowered = output.lower()

        if "cookies" in lowered:
            print("[EXPIRED] cookies invalid")
            break
        if timed_out:
            removed = discard_partial(out_dir, item_id)
            timed_out_count += 1
            failed += 1
            print(f"[TIMEOUT] item {item_id}: timeout over {args.download_timeout}s,"
                  f"Halted" + (f", cleaned {len(removed)} files" if removed else ""))
        elif returncode != 0:
            failed += 1
            print(f"[FAILED] item {item_id} (exit {returncode}): {output}")
        elif not already_downloaded(out_dir, item_id):
            if "no subtitles for the requested languages" in lowered:
                if (not args.no_transcribe and transcribed < args.max_transcribe
                        and try_transcribe(url, cookies_path, out_dir, item_id,
                                           orig_lang, args)):
                    transcribed += 1
                    succeeded += 1
                else:
                    no_subs += 1
                    item["summary"] = " "
            else:
                failed += 1
                print(f"[FAILED] item {item_id} (exit 0): {output}")
        else:
            succeeded += 1

        processed += 1

    if no_subs:
        archive_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(f"Done. succeeded={succeeded} (asr={transcribed}) failed={failed} "
          f"no_subs={no_subs} timed_out={timed_out_count}")


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
