"""
Conference reference utilities
================================
Shared by recruiting_scraper.py, roster_construction_scraper.py,
compute_conference_record.py, and the dashboard.

team_conference_<season>.csv holds Season, Team, Conference for EVERY
team that shows up in a parsed conference screenshot -- not just user
teams. Both recruiting_scraper.py and roster_construction_scraper.py
already have this data on hand (Gemini reads the whole conference
screenshot, CPU teams included), it's just filtered out before it
reaches recruiting_ranks_<season>.csv / roster_construction_<season>.csv.
This module captures it separately, without changing either of those
existing user-only files.

Per-season, since conference realignment can happen between seasons in
this dynasty -- a team's 2026 conference should never silently apply
to 2027.

Conflicts (the same team showing a different conference within one
season, e.g. a vision misread or a stale re-upload) are surfaced to
the caller rather than resolved silently -- consistent with this
project's "never a silent guess" convention. Last-write-wins on disk
(same as upsert_rows() elsewhere), but the caller gets a list of what
changed so it can log it.
"""

import csv
import os

CSV_HEADER = ["Season", "Team", "Conference"]


def _path(season: int, directory: str = ".") -> str:
    return os.path.join(directory, f"team_conference_{season}.csv")


def load_team_conference_map(season: int, directory: str = ".") -> dict:
    """Returns {team_name: conference} for the given season. Empty dict
    if the file doesn't exist yet (e.g. no recruiting/roster screenshots
    have ever been processed for that season)."""
    path = _path(season, directory)
    if not os.path.exists(path):
        return {}
    with open(path, "r", newline="", encoding="utf-8") as f:
        return {row["Team"]: row["Conference"] for row in csv.DictReader(f)}


def upsert_team_conference_rows(season: int, rows: list[dict], directory: str = ".") -> tuple[int, list[dict]]:
    """Merges rows (list of {"Team": ..., "Conference": ...}) into
    team_conference_<season>.csv, keyed on Team. Returns
    (total_rows_in_file, conflicts) where conflicts is a list of
    {"Team", "Old_Conference", "New_Conference"} for any team whose
    conference actually changed on this run -- callers should log
    these loudly rather than swallow them, since it usually means a
    vision misread rather than real realignment mid-season."""
    path = _path(season, directory)
    existing: dict[str, str] = {}
    if os.path.exists(path):
        with open(path, "r", newline="", encoding="utf-8") as f:
            existing = {row["Team"]: row["Conference"] for row in csv.DictReader(f)}

    conflicts = []
    for row in rows:
        team = row["Team"].strip()
        conf = row["Conference"].strip()
        if not team or not conf:
            continue
        if team in existing and existing[team] != conf:
            conflicts.append({"Team": team, "Old_Conference": existing[team], "New_Conference": conf})
        existing[team] = conf

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        for team in sorted(existing.keys()):
            writer.writerow({"Season": season, "Team": team, "Conference": existing[team]})

    return len(existing), conflicts
