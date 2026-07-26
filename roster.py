"""
roster.py
==========
Shared loader for Server_Members_Teams.csv -- the canonical mapping
between Discord identity (username, display name, numeric user ID) and
which team each active league member controls.

Used by:
  - schedule_scraper.py, for team <-> user matching (replaces the old
    hardcoded USER_TEAMS_BY_SEASON dict)
  - the RTA / advance-tracking Discord scripts, for tagging people by
    numeric user ID (the only thing Discord actually accepts for a real
    @mention) and for matching incoming messages by author ID rather than
    by name, which is far more robust
  - dynasty_logic.py / the dashboard, for showing each team's current
    display_name instead of whatever free-text the scraper's vision step
    happened to read off a screenshot

No season column exists in this file (unlike dynasty_data_<season>.csv),
so it's treated as "the current roster." If team assignments change
season to season, drop in a fresh Server_Members_Teams_<season>.csv (that
naming is checked first, see find_roster_csv) rather than editing this
one in place.
"""
import csv as _csv
import glob
import os
import re


def find_roster_csv(directory: str = ".") -> str | None:
    """Prefers a season-specific file (Server_Members_Teams_2026.csv) if
    present, falling back to the plain Server_Members_Teams.csv."""
    season_specific = sorted(glob.glob(os.path.join(directory, "Server_Members_Teams_*.csv")))
    if season_specific:
        def season_num(path):
            m = re.search(r"Server_Members_Teams_(\d+)\.csv$", os.path.basename(path))
            return int(m.group(1)) if m else -1
        return max(season_specific, key=season_num)
    plain = os.path.join(directory, "Server_Members_Teams.csv")
    return plain if os.path.exists(plain) else None


def load_roster(path: str) -> list[dict]:
    """Returns one dict per ACTIVE user with a team assigned (rows with
    Active User != Yes or a blank Team Name -- old players, other bots,
    server members not currently in the league -- are skipped). Each dict:
    username, display_name, user_id (str), team, nickname."""
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = _csv.DictReader(f)
        for row in reader:
            active = (row.get("Active User") or "").strip().lower() == "yes"
            team = (row.get("Team Name") or "").strip()
            if not active or not team:
                continue
            rows.append({
                "username": (row.get("username") or "").strip(),
                "display_name": (row.get("display_name") or "").strip(),
                "user_id": (row.get("user_id") or "").strip(),
                "team": team,
                "nickname": (row.get("nickname") or "").strip(),
            })
    return rows


def team_to_username(roster: list) -> dict:
    return {r["team"]: r["username"] for r in roster}


def team_to_display_name(roster: list) -> dict:
    return {r["team"]: r["display_name"] for r in roster}


def team_to_user_id(roster: list) -> dict:
    return {r["team"]: r["user_id"] for r in roster}


def user_id_to_team(roster: list) -> dict:
    return {r["user_id"]: r["team"] for r in roster}


def username_to_team(roster: list) -> dict:
    return {r["username"]: r["team"] for r in roster}


def mention(user_id: str) -> str:
    """Builds a real Discord @mention from a numeric user ID."""
    return f"<@{user_id}>"
