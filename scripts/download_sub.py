#!/usr/bin/env python3

from __future__ import annotations

import argparse, json, subprocess
from pathlib import Path


def is_youtube(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def already_downloaded(out_dir: Path, item_id: str) -> bool:
    return any(out_dir.glob(f"{item_id}*.vtt"))


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

    data = json.loads(archive_path.read_text(encoding="utf-8"))

    items = data.get("items", [])

    processed = 0

    for item in items:
        if processed >= args.max_items:
            break

        url = item.get("url", "")
        item_id = item.get("id")

        if not item_id or not is_youtube(url):
            continue
        if already_downloaded(out_dir, item_id):
            continue

        output_tpl = str(out_dir / f"{item_id}.%(language)s.%(ext)s")

        cmd = [
            "yt-dlp",
            "--cookies",
            str(Path(args.cookies_path)),
            "--user-agent",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "--extractor-args",
            "youtube:player_client=android",
            "--skip-download",
            "--write-sub",
            "--write-auto-sub",
            "--sub-langs",
            "en,zh.*",
            "--sub-format",
            "vtt",
            "-o",
            output_tpl,
            url,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        output = result.stderr + result.stdout
        if result.returncode != 0:
            if "cookies" in output.lower():
                print("[EXPIRED] cookies invalid")

        processed += 1

if __name__ == "__main__":
    main()
