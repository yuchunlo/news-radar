#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

YTDLP_COMMON_ARGS = [
    "--user-agent", USER_AGENT,
    "--extractor-args", "youtube:player_client=web",
    "--remote-components", "ejs:github",
    "--ignore-no-formats",
    "--no-progress",
]


def is_youtube(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def already_downloaded(out_dir: Path, item_id: str) -> bool:
    return any(out_dir.glob(f"{item_id}*.vtt"))


def rank_lang(lang: str, orig_lang: str | None) -> int:
    """Lower is better. See module docstring for the priority rules."""
    CHAINED_RE = re.compile(r"^zh-(hant|hans)-.+", re.I)
    ZH_PLAIN = {"zh", "zh-hant", "zh-hans", "zh-hk", "zh-tw"}
    lang_l = lang.lower()
    if CHAINED_RE.match(lang_l):
        return 3
    if orig_lang and lang_l == orig_lang.lower():
        return 0
    if lang_l in ZH_PLAIN:
        return 1
    return 2


def choose_track(manual: dict, auto: dict, orig_lang: str | None):
    """Return (is_manual, lang), or None if nothing is available at all."""
    candidates = [
        (rank_lang(lang, orig_lang), 0, lang, True) for lang in manual
    ] + [
        (rank_lang(lang, orig_lang), 1, lang, False) for lang in auto
    ]
    if not candidates:
        return None
    # Tiebreak order: rank tier, then manual-before-auto, then language code
    # (for determinism across runs).
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    _, _, lang, is_manual = candidates[0]
    return is_manual, lang


def probe_subtitle_langs(url: str, cookies_path: Path):
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
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = (result.stdout or "") + (result.stderr or "")
    if "cookies" in output.lower():
        return "EXPIRED", None, None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        # --dump-json prints one JSON object per line; take the last line in
        # case anything else got mixed into stdout.
        info = json.loads(result.stdout.strip().splitlines()[-1])
    except Exception:
        return None
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    orig_lang = info.get("language")
    return manual, auto, orig_lang


def download_one_subtitle(
    url: str, cookies_path: Path, output_tpl: str, lang: str, is_manual: bool
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
    return subprocess.run(cmd, capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--cookies-path", required=True)
    parser.add_argument("--max-items", type=int, default=70)
    args = parser.parse_args()

    archive_path = Path(args.archive)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cookies_path = Path(args.cookies_path)

    data = json.loads(archive_path.read_text(encoding="utf-8"))
    items = data.get("items", [])

    processed = 0
    succeeded = 0
    failed = 0
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

        probe = probe_subtitle_langs(url, cookies_path)
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
            no_subs += 1
            item["summary"] = " "
            processed += 1
            continue

        is_manual, lang = choice
        output_tpl = str(out_dir / f"{item_id}.%(language)s.%(ext)s")
        result = download_one_subtitle(url, cookies_path, output_tpl, lang, is_manual)
        output = (result.stdout or "") + (result.stderr or "")
        lowered = output.lower()

        if "cookies" in lowered:
            print("[EXPIRED] cookies invalid")
            break
        if result.returncode != 0:
            failed += 1
            print(f"[FAILED] item {item_id} (exit {result.returncode}): {output}")
        elif not already_downloaded(out_dir, item_id):
            if "no subtitles for the requested languages" in lowered:
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

    print(f"Done. succeeded={succeeded} failed={failed} no_subs={no_subs}")


if __name__ == "__main__":
    main()
