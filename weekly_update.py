"""
Weekly Update Poster
======================
Manually triggered (workflow_dispatch only, no schedule -- run this once
you've confirmed all of a week's data is in: schedule screenshots,
playoff bracket, recruiting rankings). Posts THREE separate Components
V2 messages to WEEKLY_UPDATE_CHANNEL_ID -- Power Rankings, CFP Rankings,
and Recruiting Rankings.

HISTORY (see git log for the actual code at each stage):
  1. Started as embeds -- looked bad, code-block tables inside embed
     descriptions wrapped badly on Discord's narrower embed rendering.
  2. Moved to plain-content messages with a monospace code-block table.
     Fixed the wrapping, but Discord doesn't parse markdown -- mentions
     AND custom emoji included -- inside a backtick code block, so
     neither tags nor logos could render there.
  3. Dropped the code block for a plain markdown list with real
     @mentions per team and custom-emoji logos. Worked, but 18-25 rows
     each with a colored mention pill read as a "wall of text."
  4. Dropped per-team mentions entirely in favor of one @everyone ping
     on the closing line -- much less visual noise.
  5. Switched to Components V2 (Container/Section/Thumbnail) for a real
     logo IMAGE next to each row instead of a tiny inline emoji
     character. Looked good, but the Thumbnail accessory has no size
     control at all (confirmed against Discord's own component
     reference docs) -- it renders at Discord's fixed ~80px accessory
     size, no matter what, and that read as too large.
  6. THIS VERSION: kept the Components V2 Container/Text Display
     structure (still gets the clean header + separator layout), but
     swapped Section+Thumbnail cards back out for inline custom emoji
     (team_emoji_map.json, from upload_team_emoji.py) inside plain Text
     Display markdown -- same small ~20px icon size as the very first
     version, since a Text Display isn't a code block and custom emoji
     tokens render fine there. Trade-off: back to "icon inline with
     text" instead of the "text left, image right" card look.

Since a row no longer costs a Section+Thumbnail (3 components), just
one line of text inside a shared Text Display, there's no longer any
real component-budget pressure -- every row in every section gets its
logo, no top-N cutoff needed like the Thumbnail version required.

This is newer, less battle-tested API surface than a lot of this
project -- built from Discord's documented schema, not verified
end-to-end here (this dev environment can't reach discord.com to test a
live post). The real test is running it for real.

Uses dynasty_logic.py directly (unlike the scrapers, which deliberately
stay standalone) -- this script's whole job is reporting numbers the
dashboard already computes, so importing the same rating pipeline is
what keeps these numbers from ever drifting out of sync with what's
shown on the dashboard.

Any section with no data yet (e.g. no Top 25 poll posted this season, no
recruiting screenshots processed) is silently skipped rather than
posting a broken message -- you'll just get fewer than 3 messages.

Required environment variables:
    DISCORD_TOKEN                Bot token (same one everything else uses)
    WEEKLY_UPDATE_CHANNEL_ID     Channel to post to (a test channel for now)

Optional:
    --season CLI flag            Defaults to the newest season with a
                                  dynasty_data_<season>.csv on disk.
"""
import glob
import logging
import os
import random
import re
import sys

import pandas as pd

import dynasty_logic as dl
import notify_utils as notify
import roster

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)-5s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("weekly_update")
notify.setup_log_capture()

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
CHANNEL_ID = os.environ.get("WEEKLY_UPDATE_CHANNEL_ID")

DASHBOARD_URL = "https://ti-dynasty-dashboard-2027.streamlit.app/"

DASHBOARD_FOOTERS = [
    "This week's updates are in! Full breakdown (and the stuff that didn't fit here) is always on the dashboard: {url}",
    "Curious how the sausage gets made? All the receipts live here: {url}",
    "Want the deeper cut? Standings, records, and more await: {url}",
    "There's more where that came from — dig in at the dashboard: {url}",
    "That's the highlight reel. Full stats on the dashboard: {url}",
]


def pick_dashboard_footer() -> str:
    return "@everyone " + random.choice(DASHBOARD_FOOTERS).format(url=DASHBOARD_URL)


# Component type numbers, per Discord's component reference:
# https://discord.com/developers/docs/components/reference
TYPE_TEXT_DISPLAY = 10
TYPE_SEPARATOR = 14
TYPE_CONTAINER = 17

CONTAINER_ACCENT_COLOR = 0xD4AF37  # gold, matches the dashboard's user-highlight accent


def get_current_season(directory: str = ".") -> int | None:
    """Deliberately duplicated (not imported from rta_logic.py/the
    scrapers) -- same standalone-script reasoning as elsewhere in this
    project, even though this file already imports dynasty_logic.py for
    the rating math specifically."""
    files = glob.glob(os.path.join(directory, "dynasty_data_*.csv"))
    if not files:
        return None

    def season_num(path):
        m = re.search(r"dynasty_data_(\d+)\.csv$", os.path.basename(path))
        return int(m.group(1)) if m else -1
    latest = max(files, key=season_num)
    n = season_num(latest)
    return n if n != -1 else None


def resolve_season() -> int | None:
    for i, arg in enumerate(sys.argv):
        if arg == "--season" and i + 1 < len(sys.argv):
            return int(sys.argv[i + 1])
    return get_current_season(".")


def _row_line(rank: int, team: str, value_label: str, team_emoji: dict) -> str:
    emoji = team_emoji.get(team, "")
    prefix = f"{emoji} " if emoji else ""
    return f"**{rank}.** {prefix}{team} — {value_label}"


def _build_container(title: str, rows: list, team_emoji: dict) -> dict:
    """rows: list of (rank, team, value_label) tuples, already sorted.
    Every row is one line inside a single shared Text Display -- no
    per-row Section/Thumbnail anymore (see module docstring for why),
    so there's no component-budget concern regardless of row count."""
    lines = [_row_line(rank, team, value_label, team_emoji) for rank, team, value_label in rows]
    components = [
        {"type": TYPE_TEXT_DISPLAY, "content": f"## {title}"},
        {"type": TYPE_SEPARATOR},
        {"type": TYPE_TEXT_DISPLAY, "content": "\n".join(lines)},
    ]
    return {"type": TYPE_CONTAINER, "accent_color": CONTAINER_ACCENT_COLOR, "components": components}


def build_power_rankings_container(season: int, team_emoji: dict, directory: str = ".") -> dict | None:
    path = os.path.join(directory, f"dynasty_data_{season}.csv")
    if not os.path.exists(path):
        log.warning("No dynasty_data_%d.csv found -- skipping Power Rankings.", season)
        return None

    df = dl.load_and_prepare(path)
    teams = sorted(df["Team"].unique())
    if not teams:
        log.warning("No teams found in dynasty_data_%d.csv -- skipping Power Rankings.", season)
        return None

    rank_basis = "at_game" if dl.has_at_game_rank_data(df) else "live"
    stats = dl.compute_team_stats(df, teams, rank_basis=rank_basis)
    stats = dl.add_strength_of_schedule(df, stats, rank_basis=rank_basis)
    rated = dl.compute_dynasty_rating(stats, dict(dl.DEFAULT_RATING_WEIGHTS))

    completed = df[df["Completed"]]
    week_label = "—"
    if not completed.empty:
        latest_week_sort = completed["Week_Sort"].max()
        week_label = completed.loc[completed["Week_Sort"] == latest_week_sort, "Week"].iloc[0]

    rows = [(int(row["Rank"]), team, f"{row['Dynasty_Rating']:.1f}") for team, row in rated.iterrows()]
    return _build_container(f"🏆 Power Rankings — Week {week_label}", rows, team_emoji)


def build_cfp_rankings_container(season: int, team_emoji: dict, directory: str = ".") -> dict | None:
    path = os.path.join(directory, f"top25_rankings_{season}.csv")
    if not os.path.exists(path):
        log.warning("No top25_rankings_%d.csv found -- skipping CFP Rankings.", season)
        return None
    try:
        top25 = pd.read_csv(path)
    except (pd.errors.EmptyDataError, OSError):
        log.warning("top25_rankings_%d.csv is empty/unreadable -- skipping CFP Rankings.", season)
        return None
    if top25.empty:
        return None

    top25 = top25.sort_values("Rank")
    rows = [(int(r["Rank"]), r["Team"], r["Record"]) for _, r in top25.iterrows()]
    return _build_container("🏈 CFP Rankings", rows, team_emoji)


def build_recruiting_container(season: int, team_emoji: dict, directory: str = ".") -> dict | None:
    path = os.path.join(directory, f"recruiting_ranks_{season}.csv")
    if not os.path.exists(path):
        log.warning("No recruiting_ranks_%d.csv found -- skipping Recruiting Rankings.", season)
        return None
    try:
        rec = pd.read_csv(path)
    except (pd.errors.EmptyDataError, OSError):
        log.warning("recruiting_ranks_%d.csv is empty/unreadable -- skipping Recruiting Rankings.", season)
        return None
    if rec.empty:
        return None

    rec = rec.sort_values("National_Rank")
    rows = [(int(r["National_Rank"]), r["Team"], f"{int(r['Total_Commits'])} commits") for _, r in rec.iterrows()]
    return _build_container(f"🎯 Recruiting Rankings — {season} Class", rows, team_emoji)


def _append_footer(container: dict) -> None:
    """Mutates container in place, adding the @everyone + dashboard line
    as a closing Separator + Text Display -- same "one ping for the
    whole report" idea as the earlier plain-text version, just expressed
    as components instead of appended message text."""
    container["components"].append({"type": TYPE_SEPARATOR})
    container["components"].append({"type": TYPE_TEXT_DISPLAY, "content": pick_dashboard_footer()})


def main():
    missing = [name for name, val in [("DISCORD_TOKEN", DISCORD_TOKEN), ("WEEKLY_UPDATE_CHANNEL_ID", CHANNEL_ID)] if not val]
    if missing:
        log.error("Missing environment variable(s): %s", ", ".join(missing))
        sys.exit(1)

    season = resolve_season()
    if season is None:
        log.error("No dynasty_data_<season>.csv found anywhere -- nothing to report.")
        sys.exit(1)
    log.info("Building weekly update for season %d.", season)

    team_emoji = roster.load_team_emoji_map(".")

    containers = []
    power = build_power_rankings_container(season, team_emoji)
    if power:
        containers.append(power)
    cfp = build_cfp_rankings_container(season, team_emoji)
    if cfp:
        containers.append(cfp)
    recruiting = build_recruiting_container(season, team_emoji)
    if recruiting:
        containers.append(recruiting)

    if not containers:
        log.warning("Nothing to post -- no section had usable data.")
        notify.post_alert(CHANNEL_ID, DISCORD_TOKEN, "⚠️ Weekly update: no data available for any section, nothing posted.")
        sys.exit(0)

    _append_footer(containers[-1])

    for i, container in enumerate(containers):
        is_last = i == len(containers) - 1
        log.info("Posting message %d/%d...", i + 1, len(containers))
        notify.post_components_v2(CHANNEL_ID, DISCORD_TOKEN, [container], allow_everyone_ping=is_last)
    log.info("Posted %d message(s) to channel %s.", len(containers), CHANNEL_ID)


if __name__ == "__main__":
    main()
