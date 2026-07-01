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
    args = parser.parse_args()

    archive_path = Path(args.archive)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    data = json.loads(archive_path.read_text(encoding="utf-8"))

    items = data.get("items", [])

    for item in items:
        url = item.get("url", "")
        item_id = item.get("id")

        if not item_id or not is_youtube(url):
            continue
        if already_downloaded(out_dir, item_id):
            continue

        output_tpl = str(out_dir / f"{item_id}.%(language)s.%(ext)s")

        cmd = [
            "yt-dlp",
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

        subprocess.run(cmd, check=False)


if __name__ == "__main__":
    main()
