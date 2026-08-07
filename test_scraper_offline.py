"""
Offline Scraper Test
=====================
Lets you test what schedule_scraper.py would extract from a screenshot
WITHOUT needing Discord at all -- just a local image file and a real
Gemini API key. Useful for checking the vision extraction is accurate
before ever posting a screenshot to the actual bot, or for debugging a
screenshot that failed/produced weird results in production.

This deliberately reuses schedule_scraper.py's actual vision-calling
function directly (same prompt, same model chain, same retry logic)
rather than a separate copy -- so what you see here is a real preview of
what the live bot would do, not an approximation that could drift out of
sync with the real thing over time.

Usage:
    export GENAI_API_KEY=your_real_key
    python test_scraper_offline.py path/to/screenshot.png

    # Also preview how it would merge into an existing season CSV,
    # without actually writing anything:
    python test_scraper_offline.py path/to/screenshot.png --merge-preview dynasty_data_2026.csv
"""
import argparse
import asyncio
import os
import sys

# schedule_scraper.py requires DISCORD_TOKEN and SCREENSHOT_CHANNEL_ID to
# even import (it validates them at module load time, for the real bot's
# sake) -- neither is actually needed for this offline test, so set safe
# placeholders if they're not already set, rather than requiring you to
# have real Discord credentials just to test a screenshot locally.
os.environ.setdefault("DISCORD_TOKEN", "offline-test-placeholder")
os.environ.setdefault("SCREENSHOT_CHANNEL_ID", "0")

if not os.environ.get("GENAI_API_KEY"):
    print("ERROR: GENAI_API_KEY must be set to a real key -- this is the one thing")
    print("this test genuinely needs, since it's actually calling the vision model.")
    sys.exit(1)

import schedule_scraper as ss  # noqa: E402  (import after setting the env vars above)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="Path to a local screenshot image file")
    parser.add_argument(
        "--merge-preview", metavar="CSV_PATH", default=None,
        help="Optional: an existing dynasty_data_<season>.csv to preview merging into (read-only, never writes)",
    )
    parser.add_argument(
        "--season", type=int, default=None,
        help="Season to use for team-name matching. Defaults to the SEASON env var. "
             "Required one way or another -- NOT prompted for interactively, since that would "
             "silently hang this script if run non-interactively.",
    )
    args = parser.parse_args()

    season = args.season or (int(os.environ["SEASON"]) if os.environ.get("SEASON") else None)
    if season is None:
        print("ERROR: no season specified. Pass --season 2026 or set the SEASON env var.")
        print("(Deliberately not falling back to the real scraper's interactive prompt here --")
        print(" that would silently hang this script if you're not at an actual terminal.)")
        sys.exit(1)

    if not os.path.exists(args.image):
        print(f"ERROR: {args.image} not found.")
        sys.exit(1)

    with open(args.image, "rb") as f:
        image_bytes = f.read()

    print(f"Sending {args.image} to the vision model (same prompt/model chain as the real scraper)...")
    print()
    data = await ss.parse_schedule_image_with_vision(image_bytes, os.path.basename(args.image))

    if data is None:
        print("FAILED -- the vision model couldn't extract usable data from this image.")
        print("Check the log output above for the specific error (bad JSON, API error, etc.).")
        sys.exit(1)

    print("=" * 70)
    print("RAW EXTRACTED DATA")
    print("=" * 70)
    print(f"Featured team: {data.get('featured_team', 'Unknown')}")
    schedule_rows = data.get("schedule", [])
    print(f"Schedule rows extracted: {len(schedule_rows)}")
    print()
    for row in schedule_rows:
        print(" ", row)

    # process_vision_data() needs USER_CONTROLLED_TEAMS set up, which the
    # real bot only does during its own startup sequence -- replicate that
    # one step here rather than duplicating the team-matching logic itself.
    ss.USER_TEAMS = ss.resolve_user_teams(season)
    ss.USER_CONTROLLED_TEAMS = set(ss.USER_TEAMS.values())
    ss.TEAM_TO_USER = {team: user for user, team in ss.USER_TEAMS.items()}

    print()
    print("=" * 70)
    print("PROCESSED ROWS (what would actually get written)")
    print("=" * 70)
    processed = ss.process_vision_data(data)
    for row in processed:
        print(" ", row)

    if args.merge_preview:
        print()
        print("=" * 70)
        print(f"MERGE PREVIEW against {args.merge_preview} (read-only, nothing written)")
        print("=" * 70)
        if not os.path.exists(args.merge_preview):
            print(f"  {args.merge_preview} doesn't exist -- would be created fresh with these {len(processed)} row(s).")
        else:
            import pandas as pd
            existing = pd.read_csv(args.merge_preview, engine="python", on_bad_lines="skip", dtype=str)
            new_df = pd.DataFrame(processed)
            # Reuse the season embedded in the CSV's own filename if
            # possible, matching how the real scraper determines it.
            import re
            m = re.search(r"dynasty_data_(\d+)\.csv$", os.path.basename(args.merge_preview))
            csv_season = int(m.group(1)) if m else season
            merged = ss.merge_records(existing, new_df, csv_season)
            added_or_changed = len(merged) - len(existing)
            print(f"  Existing rows: {len(existing)}")
            print(f"  Rows after merge: {len(merged)}  ({'+' if added_or_changed >= 0 else ''}{added_or_changed})")
            print("  (This preview does not write to disk -- rerun the real scraper to actually apply it.)")


if __name__ == "__main__":
    asyncio.run(main())
