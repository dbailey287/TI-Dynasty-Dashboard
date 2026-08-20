"""
Weekly Update Poster
======================
Manually triggered (workflow_dispatch only, no schedule -- run this once
you've confirmed all of a week's data is in: schedule screenshots,
playoff bracket, recruiting rankings). Posts one Discord message
containing three rich embeds -- Power Rankings, CFP Rankings, and
Recruiting Rankings -- to WEEKLY_UPDATE_CHANNEL_ID.

Embeds, not plain content, because a single plain-text message caps at
2000 characters and three ~18-row tables plus headers gets close to that
fast. Each embed gets its own much larger budget (up to 4096 chars in
its description alone), so nothing needs trimming to fit.

Uses dynasty_logic.py directly (unlike the scrapers, which deliberately
stay standalone) -- this script's whole job is reporting numbers the
dashboard already computes, so importing the same rating pipeline is
what keeps these numbers from ever drifting out of sync with what's
shown on the dashboard.

Any section with no data yet (e.g. no Top 25 poll posted this season, no
recruiting screenshots processed) is silently skipped rather than
posting an empty/broken embed -- you'll just get fewer than 3 embeds.

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

COLOR_POWER = 0xD4AF37   # gold, matches the dashboard's user-highlight accent
COLOR_CFP = 0xC0392B     # red
COLOR_RECRUITING = 0x2E86C1  # blue


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


def build_power_rankings_embed(season: int, directory: str = ".") -> dict | None:
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

    lines = []
    for team, row in rated.iterrows():
        lines.append(f"{int(row['Rank']):>2}. {team:<18} {row['Dynasty_Rating']:>5.1f}")

    return {
        "title": f"🏆 Power Rankings — Week {week_label}",
        "description": "```\n" + "\n".join(lines) + "\n```",
        "color": COLOR_POWER,
    }


def build_cfp_rankings_embed(season: int, team_to_display: dict, directory: str = ".") -> dict | None:
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
    # User-controlled teams get their display name tagged on; a CPU-only
    # team (not in team_to_display) just shows the team name, same as
    # before -- this is what actually distinguishes "one of our teams" at
    # a glance instead of every row looking identical.
    team_and_user = []
    for _, r in top25.iterrows():
        display_name = team_to_display.get(r["Team"], "")
        team_and_user.append(f"{r['Team']} {display_name}".rstrip() if display_name else r["Team"])
    col_width = max((len(s) for s in team_and_user), default=18)

    lines = [
        f"{int(r['Rank']):>2}. {label:<{col_width}} {r['Record']:>6}"
        for (_, r), label in zip(top25.iterrows(), team_and_user)
    ]

    return {
        "title": "🏈 CFP Rankings",
        "description": "```\n" + "\n".join(lines) + "\n```",
        "color": COLOR_CFP,
    }


def build_recruiting_embed(season: int, directory: str = ".") -> dict | None:
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
    lines = [
        f"{int(r['National_Rank']):>3}. {r['Team']:<18} {int(r['Total_Commits']):>2} commits"
        for _, r in rec.iterrows()
    ]

    return {
        "title": f"🎯 Recruiting Rankings — {season} Class",
        "description": "```\n" + "\n".join(lines) + "\n```",
        "color": COLOR_RECRUITING,
    }


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

    embeds = []

    power_embed = build_power_rankings_embed(season)
    if power_embed:
        embeds.append(power_embed)

    roster_entries = roster.load_roster(roster.find_roster_csv(".")) if roster.find_roster_csv(".") else []
    team_to_display = roster.team_to_display_name(roster_entries)
    cfp_embed = build_cfp_rankings_embed(season, team_to_display)
    if cfp_embed:
        embeds.append(cfp_embed)

    recruiting_embed = build_recruiting_embed(season)
    if recruiting_embed:
        embeds.append(recruiting_embed)

    if not embeds:
        log.warning("Nothing to post -- no section had usable data.")
        notify.post_alert(CHANNEL_ID, DISCORD_TOKEN, "⚠️ Weekly update: no data available for any section, nothing posted.")
        sys.exit(0)

    notify.post_embeds(CHANNEL_ID, DISCORD_TOKEN, embeds)
    log.info("Posted %d embed(s) to channel %s.", len(embeds), CHANNEL_ID)


if __name__ == "__main__":
    main()
