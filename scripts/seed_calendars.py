#!/usr/bin/env python3
"""Seed Firestore (config/display) with the calendar list from a local JSON.

Usage:
    python scripts/seed_calendars.py [path-to-json]

Defaults to scripts/calendars.local.json (gitignored). Hard-codes the GCP
project so it always writes to talker-491708 regardless of your active
gcloud config.
"""

import json
import os
import sys

# Force Firestore client to talker-491708 even if `gcloud config` points
# elsewhere — your default gcloud project may be a different one.
os.environ["GOOGLE_CLOUD_PROJECT"] = "talker-491708"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "server"))

import db  # noqa: E402

DEFAULT_PATH = os.path.join(HERE, "calendars.local.json")


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH
    if not os.path.exists(path):
        print(f"error: {path} not found", file=sys.stderr)
        print(f"create it from the example shape, or pass a different path",
              file=sys.stderr)
        return 1

    with open(path) as f:
        cfg = json.load(f)

    if not isinstance(cfg.get("calendars"), list):
        print("error: JSON must have a top-level 'calendars' list",
              file=sys.stderr)
        return 1

    if not db.USE_FIRESTORE:
        print("error: Firestore client unavailable — check GCP auth",
              file=sys.stderr)
        return 1

    db.set_display_config(cfg)
    cals = cfg["calendars"]
    print(f"seeded {len(cals)} calendar(s) into config/display "
          f"(project=talker-491708)")
    for c in cals:
        url_short = c.get("url", "")[:60] + ("…" if len(c.get("url", "")) > 60 else "")
        print(f"  - {c.get('name', '?')!r:14s} filter={c.get('filter', '?')!r:30s} url={url_short}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
