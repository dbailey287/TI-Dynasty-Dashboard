"""
Weekly Update Poster
======================
Manually triggered (workflow_dispatch only, no schedule -- run this once
you've confirmed all of a week's data is in: schedule screenshots,
playoff bracket, recruiting rankings). Posts THREE separate plain-text
messages to WEEKLY_UPDATE_CHANNEL_ID -- Power Rankings, CFP Rankings,
and Recruiting Rankings.

Three separate messages, not one message with embeds (an earlier version
of this script used embeds -- see git history if that's ever wanted
back). Two reasons for the change:
  1. Discord doesn't parse markdown -- @mentions included -- inside a
     backtick code block, whether that block lives in plain content or
     an embed description. CFP Rankings needs real, clickable/pingable
     @mentions for user-controlled teams, so that section can't be a
     code-block table at all.
  2. Once CFP Rankings drops the aligned table format, there's no longer
     a strong reason to keep the other two sections bundled into a
     single message either -- three separate messages are simpler and
     each stands alone in channel history.

Power Rankings, CFP Rankings, and Recruiting Rankings are all plain
markdown lists now (no code-block tables) -- both real <@user_id>
mentions AND custom team-logo emoji only render inside plain message
text, never inside a backtick code block, which Discord treats as
literal unstyled text. CFP Rankings additionally tags user-controlled
teams with a real mention via roster.py -- rendered as a visible tag,
but NOT pinging by default (see notify.post_message's allow_pings)
since this is a recurring recap, not an urgent alert.

Team logos come from team_emoji_map.json (custom Discord server emoji,
see upload_team_emoji.py) -- only your user-controlled teams have one
uploaded, so CPU opponents in CFP Rankings just show plain text.

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

# Whether CFP Rankings' user mentions actually notify people, or just
# show as a clickable tag. True = real ping/notification.
CFP_MENTIONS_PING = True


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


def build_power_rankings_message(season: int, team_emoji: dict, directory: str = ".") -> str | None:
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

    lines = [f"🏆 **Power Rankings — Week {week_label}**"]
    for team, row in rated.iterrows():
        logo = team_emoji.get(team, "")
        prefix = f"{logo} " if logo else ""
        lines.append(f"**{int(row['Rank'])}.** {prefix}{team} — {row['Dynasty_Rating']:.1f}")

    return "\n".join(lines)


def build_cfp_rankings_message(season: int, team_to_user_id: dict, team_emoji: dict, directory: str = ".") -> str | None:
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
    lines = ["🏈 **CFP Rankings**"]
    for _, r in top25.iterrows():
        team = r["Team"]
        logo = team_emoji.get(team, "")
        prefix = f"{logo} " if logo else ""
        user_id = team_to_user_id.get(team)
        tag = f" ({roster.mention(user_id)})" if user_id else ""
        lines.append(f"**{int(r['Rank'])}.** {prefix}{team}{tag} — {r['Record']}")

    return "\n".join(lines)


def build_recruiting_message(season: int, team_emoji: dict, directory: str = ".") -> str | None:
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
    lines = [f"🎯 **Recruiting Rankings — {season} Class**"]
    for _, r in rec.iterrows():
        team = r["Team"]
        logo = team_emoji.get(team, "")
        prefix = f"{logo} " if logo else ""
        lines.append(f"**{int(r['National_Rank'])}.** {prefix}{team} — {int(r['Total_Commits'])} commits")

    return "\n".join(lines)


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

    roster_entries = roster.load_roster(roster.find_roster_csv(".")) if roster.find_roster_csv(".") else []
    team_to_user_id = roster.team_to_user_id(roster_entries)
    team_emoji = roster.load_team_emoji_map(".")

    messages = []
    power_msg = build_power_rankings_message(season, team_emoji)
    if power_msg:
        messages.append(power_msg)
    cfp_msg = build_cfp_rankings_message(season, team_to_user_id, team_emoji)
    if cfp_msg:
        messages.append(cfp_msg)
    recruiting_msg = build_recruiting_message(season, team_emoji)
    if recruiting_msg:
        messages.append(recruiting_msg)

    if not messages:
        log.warning("Nothing to post -- no section had usable data.")
        notify.post_alert(CHANNEL_ID, DISCORD_TOKEN, "⚠️ Weekly update: no data available for any section, nothing posted.")
        sys.exit(0)

    for msg in messages:
        notify.post_message(CHANNEL_ID, DISCORD_TOKEN, msg, allow_pings=CFP_MENTIONS_PING)
    log.info("Posted %d message(s) to channel %s.", len(messages), CHANNEL_ID)


if __name__ == "__main__":
    main()
