"""
Components V2 Prototype
==========================
NOT wired into the real weekly reports (weekly_update.py). This is a
standalone, manually-triggered TEST script -- posts one sample message
using Discord's newer Components V2 layout system, so you can see what
it actually looks like before deciding whether it's worth rebuilding
weekly_update.py's three messages around it.

Why this might look cleaner than the current plain-text version: each
row uses a real Thumbnail image (a proper logo, same size/crop as
anywhere else logos show up) sitting next to the text via a Section
component, instead of a tiny inline custom emoji character crammed into
the line. Discord's own docs describe Sections as "content alongside an
accessory" -- built for exactly this "row of text + small image" layout.

Real, documented trade-offs to know before judging the result:
  - Only shows the top 8 teams, not a full 18-25 -- deliberately a small
    sample for evaluation purposes, not a production-ready replacement.
  - A Section allows at most 3 Text Display components + ONE accessory
    (thumbnail or button) -- can't cram much text per row.
  - Components V2 messages can't use the classic "content" or "embeds"
    fields at all -- it's an entirely different message shape, not an
    add-on to what weekly_update.py already does.
  - This is newer, less battle-tested API surface than the rest of this
    project -- built from Discord's documented schema
    (discord.com/developers/docs/components/reference), but this dev
    environment can't reach discord.com to verify it renders correctly.
    The real test is running this for real.

Required environment variables:
    DISCORD_TOKEN                Bot token
    WEEKLY_UPDATE_CHANNEL_ID     Same test channel as weekly_update.py

Usage:
    python test_components_v2.py [--season 2026]
"""
import logging
import os
import sys

import dynasty_logic as dl
import notify_utils as notify

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)-5s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("test_components_v2")

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
CHANNEL_ID = os.environ.get("WEEKLY_UPDATE_CHANNEL_ID")
DASHBOARD_URL = "https://ti-dynasty-dashboard-2027.streamlit.app/"

# Component type numbers, per Discord's component reference:
# https://discord.com/developers/docs/components/reference
TYPE_SECTION = 9
TYPE_TEXT_DISPLAY = 10
TYPE_THUMBNAIL = 11
TYPE_SEPARATOR = 14
TYPE_CONTAINER = 17

TOP_N = 8  # sample size for this prototype -- not the full ranking list


def resolve_season() -> int | None:
    for i, arg in enumerate(sys.argv):
        if arg == "--season" and i + 1 < len(sys.argv):
            return int(sys.argv[i + 1])
    import glob
    import re
    files = glob.glob("dynasty_data_*.csv")
    if not files:
        return None
    def season_num(path):
        m = re.search(r"dynasty_data_(\d+)\.csv$", os.path.basename(path))
        return int(m.group(1)) if m else -1
    return season_num(max(files, key=season_num))


def build_sample_container(season: int) -> dict | None:
    path = f"dynasty_data_{season}.csv"
    if not os.path.exists(path):
        log.error("No %s found.", path)
        return None

    df = dl.load_and_prepare(path)
    teams = sorted(df["Team"].unique())
    rank_basis = "at_game" if dl.has_at_game_rank_data(df) else "live"
    stats = dl.compute_team_stats(df, teams, rank_basis=rank_basis)
    stats = dl.add_strength_of_schedule(df, stats, rank_basis=rank_basis)
    rated = dl.compute_dynasty_rating(stats, dict(dl.DEFAULT_RATING_WEIGHTS)).head(TOP_N)

    components = [
        {"type": TYPE_TEXT_DISPLAY, "content": f"# 🏆 Power Rankings — Top {TOP_N} (Components V2 test)"},
        {"type": TYPE_SEPARATOR},
    ]

    for team, row in rated.iterrows():
        logo_url = dl.logo_url(team)
        section = {
            "type": TYPE_SECTION,
            "components": [
                {"type": TYPE_TEXT_DISPLAY, "content": f"**{int(row['Rank'])}. {team}** — {row['Dynasty_Rating']:.1f}"},
            ],
        }
        if logo_url:
            section["accessory"] = {"type": TYPE_THUMBNAIL, "media": {"url": logo_url}}
        components.append(section)

    components.append({"type": TYPE_SEPARATOR})
    components.append({
        "type": TYPE_TEXT_DISPLAY,
        "content": f"This is a Components V2 test post — not the real weekly report. Full dashboard: {DASHBOARD_URL}",
    })

    return {"type": TYPE_CONTAINER, "accent_color": 0xD4AF37, "components": components}


def main():
    missing = [name for name, val in [("DISCORD_TOKEN", DISCORD_TOKEN), ("WEEKLY_UPDATE_CHANNEL_ID", CHANNEL_ID)] if not val]
    if missing:
        log.error("Missing environment variable(s): %s", ", ".join(missing))
        sys.exit(1)

    season = resolve_season()
    if season is None:
        log.error("No dynasty_data_<season>.csv found anywhere.")
        sys.exit(1)

    container = build_sample_container(season)
    if not container:
        sys.exit(1)

    log.info("Posting Components V2 test message (top %d teams, season %d)...", TOP_N, season)
    notify.post_components_v2(CHANNEL_ID, DISCORD_TOKEN, [container])
    log.info("Sent. Check the test channel.")


if __name__ == "__main__":
    main()
