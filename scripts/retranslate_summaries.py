#!/usr/bin/env python3
"""Run the language backfill on its own, over an existing items file.

The logic lives in summarize_feed.backfill, which the normal run
calls as pass 3 once its summaries are done. This is that same pass with no
fetching in front of it -- for backfilling history in one go, or for retrying
after a run stopped at its time budget. Kept as a wrapper rather than a second
implementation so the two entry points cannot drift apart.

    python3 retranslate_summaries.py archive.json --dry-run
    python3 retranslate_summaries.py archive.json --limit 50
    python3 retranslate_summaries.py archive.json -o out.json
    python3 retranslate_summaries.py archive.json --simplified-only
"""

from __future__ import annotations

import argparse
import json
import sys

import jsonio
from summarize_feed import backfill, needs_translation


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("-o", "--out", help="output path (default: in place)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    ap.add_argument("--simplified-only", action="store_true",
                    help="offline work only (thumbnails + 簡體→繁體), "
                         "never touch the network")
    ap.add_argument("--no-revalidate-thumbnails", dest="revalidate_thumbnails",
                    action="store_false", default=True,
                    help="skip the thumbnail re-check")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="translate at most N summaries (0 = no limit)")
    args = ap.parse_args(argv)

    with open(args.path, encoding="utf-8") as f:
        doc = json.load(f)
    items = doc["items"]
    out_path = args.out or args.path

    if args.dry_run:
        targets = [it for it in items if needs_translation(it.get("summary"))]
        print(f"non-Chinese summaries that would be translated: {len(targets)}")
        for it in targets[:5]:
            print(f"  {it['url'][:70]}  {it['summary'][:60]}")
        return 0

    backfill(
        items,
        translate_enabled=not args.simplified_only,
        revalidate_thumbnails=args.revalidate_thumbnails,
        limit=args.limit,
        save=lambda: jsonio.write_atomic(out_path, doc),
    )
    doc["total_items"] = len(items)
    jsonio.write_atomic(out_path, doc)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
