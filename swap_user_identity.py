"""
Swap User Identity
=====================
One-time, manually-run script for when a roster member gets a new
Discord account (locked out, renamed, etc.) but keeps the same team --
updates their identity everywhere it's stored while preserving their
full historical record as ONE continuous coach.

WHY THIS NEEDS TO TOUCH MORE THAN Server_Members_Teams.csv:
Almost everything in this project (recruiting_ranks_*.csv,
roster_construction_*.csv, conference_record_*.csv, top25_weeks_*.csv,
team_emoji_map.json) is keyed by TEAM, not by Discord identity -- none
of those need any change here.

The exception is dynasty_data_<season>.csv: every row has a "User"
column with whatever username was current AT SCRAPE TIME, baked in as
literal text. dynasty_logic.py's compute_career_stats() groups the
Career page by an exact match on that string, not by resolving identity
live through the roster -- so if only Server_Members_Teams.csv gets
updated, the Career page will show this person as two separate coaches
(old seasons under the old username, new seasons under the new one)
instead of one continuous record.

This script updates BOTH: the live roster row, and every past season's
User column for that team's rows (matched by Team, not a blind
find-and-replace, so it can't accidentally touch an unrelated row that
happens to share the old username string).

NOT touched (confirmed to store Team only, not identity):
recruiting_ranks_*.csv, roster_construction_*.csv,
conference_record_*.csv, top25_weeks_*.csv, team_emoji_map.json.

NOT handled: rta_status.json's ready_user_ids is keyed by user_id, so a
"ready" flag under the OLD account this week won't carry over to the
new one. Simplest fix if that's the case: just have them RTA again
under the new account.

Usage:
    python swap_user_identity.py --team "Arkansas" \\
        --new-username "newname123" --new-user-id "1234567890123456789" \\
        --new-display-name "New Display Name - Arkansas 🐗" \\
        [--new-nickname "..."]
"""
import argparse
import csv
import glob
import logging
import os
import re

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)-5s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("swap_user_identity")


def update_roster_csv(path: str, team: str, new_username: str, new_user_id: str,
                       new_display_name: str, new_nickname: str) -> str | None:
    """Updates the roster row for `team` in place. Returns the OLD
    username (needed for the dynasty_data backfill below), or None if no
    matching row was found."""
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
        fieldnames = list(rows[0].keys()) if rows else []

    old_username = None
    found = False
    for row in rows:
        if row.get("Team Name", "").strip() == team:
            old_username = row.get("username", "").strip()
            row["username"] = new_username
            row["user_id"] = new_user_id
            row["display_name"] = new_display_name
            if new_nickname is not None:
                row["nickname"] = new_nickname
            found = True

    if not found:
        return None

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return old_username


def backfill_dynasty_data(directory: str, team: str, old_username: str, new_username: str) -> int:
    """Updates BOTH identity columns in every dynasty_data_<season>.csv
    found: the "User" column on `team`'s own rows, AND the
    "Opponent_User" column on OTHER teams' rows from games played
    against `team` (e.g. a User vs User game shows up on both sides'
    schedules, each tagging the opposing coach). Missing the second one
    leaves a stale reference to the old identity on every opponent's row
    -- confirmed by testing against real data, not assumed: a team that
    played 19 games had only 17 "User" column matches, the other 2 were
    "Opponent_User" on the two User-vs-User opponents' own rows.
    Returns the total number of cells updated across all seasons."""
    files = sorted(glob.glob(os.path.join(directory, "dynasty_data_*.csv")))
    total_updated = 0

    for path in files:
        season_match = re.search(r"dynasty_data_(\d+)\.csv$", os.path.basename(path))
        season = season_match.group(1) if season_match else "?"

        with open(path, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            fieldnames = list(rows[0].keys()) if rows else []

        updated_this_file = 0
        for row in rows:
            if row.get("Team", "").strip() == team and row.get("User", "").strip() == old_username:
                row["User"] = new_username
                updated_this_file += 1
            if row.get("Opponent", "").strip() == team and row.get("Opponent_User", "").strip() == old_username:
                row["Opponent_User"] = new_username
                updated_this_file += 1

        if updated_this_file:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            log.info("Season %s: updated %d cell(s) in %s.", season, updated_this_file, os.path.basename(path))
            total_updated += updated_this_file
        else:
            log.info("Season %s: no rows for '%s' under old username '%s' -- nothing to update.", season, team, old_username)

    return total_updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team", required=True, help="Team name exactly as it appears in Server_Members_Teams.csv / dynasty_data (e.g. 'Arkansas')")
    parser.add_argument("--new-username", required=True)
    parser.add_argument("--new-user-id", required=True)
    parser.add_argument("--new-display-name", required=True)
    parser.add_argument("--new-nickname", default=None, help="Leave unset to keep the existing nickname unchanged")
    parser.add_argument("--directory", default=".")
    args = parser.parse_args()

    roster_path = os.path.join(args.directory, "Server_Members_Teams.csv")
    if not os.path.exists(roster_path):
        log.error("Server_Members_Teams.csv not found in %s.", args.directory)
        return

    old_username = update_roster_csv(
        roster_path, args.team, args.new_username, args.new_user_id,
        args.new_display_name, args.new_nickname,
    )
    if old_username is None:
        log.error("No row found for Team '%s' in Server_Members_Teams.csv -- check spelling. Nothing changed.", args.team)
        return
    log.info("Updated Server_Members_Teams.csv: '%s' (%s -> %s).", args.team, old_username, args.new_username)

    if not old_username:
        log.warning("Old username was blank -- skipping dynasty_data backfill (nothing to match against). "
                    "If historical rows need updating, run again manually or check the CSV by hand.")
        return

    total = backfill_dynasty_data(args.directory, args.team, old_username, args.new_username)
    log.info("Done. %d historical cell(s) updated across all seasons (User + Opponent_User columns).", total)
    log.info("Reminder: team_emoji_map.json, recruiting/roster-construction/conference-record/top25-weeks "
             "files are keyed by Team only and needed no changes.")
    log.info("Reminder: if '%s' already marked RTA-ready this week under the old account, "
             "that won't carry over (rta_status.json tracks readiness by user_id) -- have them RTA again.", args.team)


if __name__ == "__main__":
    main()
