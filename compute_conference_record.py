"""
Conference Record Calculator
=============================
Cross-references dynasty_data_<season>.csv (the schedule/results file)
against team_conference_<season>.csv (see conference_utils.py) to
compute each user team's in-conference win/loss record -- including
CPU opponents, as long as that CPU team's conference is known from a
parsed recruiting/roster screenshot.

No screenshot ever shows "conference record" directly, so this is
always derived, never scraped.

A game only counts toward conference record if BOTH the team and its
opponent have a known conference for that season AND those conferences
match. Games against an opponent with no known conference (never
appeared in a recruiting/roster screenshot that season) are silently
excluded from the conference-record tally -- not counted as a loss, a
win, or a "non-conference" game either, just unclassifiable. This is
expected to shrink over time as more conference screenshots get
uploaded each season.

Season resolution:
    --season CLI flag processes just that one season (this is what the
    daily schedule-scraper workflow passes, right after a run that
    found new schedule data).
    Omitted entirely -> recomputes EVERY season that has both a
    dynasty_data_<season>.csv and a team_conference_<season>.csv,
    which is what keeps "historical" conference records self-healing
    if team_conference data gets corrected or backfilled later.

Output: conference_record_<season>.csv per season, columns
Season, Team, Conference, Conf_W, Conf_L, Conf_Record.
Fully rebuilt (not incrementally upserted) each time it runs for a
given season -- cheap to recompute in full, and avoids any risk of
stale rows lingering after a correction.
"""

import csv
import glob
import logging
import os
import re
import sys

import pandas as pd

import conference_utils

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("conference_record")

CSV_HEADER = ["Season", "Team", "Conference", "Conf_W", "Conf_L", "Conf_Record"]


def _seasons_with_schedule_data(directory: str = ".") -> list[int]:
    files = glob.glob(os.path.join(directory, "dynasty_data_*.csv"))
    seasons = []
    for path in files:
        m = re.search(r"dynasty_data_(\d+)\.csv$", os.path.basename(path))
        if m:
            seasons.append(int(m.group(1)))
    return sorted(seasons)


def compute_for_season(season: int, directory: str = ".") -> int | None:
    """Returns the number of teams written to conference_record_<season>.csv,
    or None if the season couldn't be computed (missing schedule or
    conference data)."""
    schedule_path = os.path.join(directory, f"dynasty_data_{season}.csv")
    if not os.path.exists(schedule_path):
        log.warning("No dynasty_data_%d.csv found, skipping season %d.", season, season)
        return None

    team_conf = conference_utils.load_team_conference_map(season, directory)
    if not team_conf:
        log.warning(
            "No team_conference_%d.csv found (or it's empty) -- run recruiting_scraper.py or "
            "roster_construction_scraper.py for season %d first. Skipping.", season, season,
        )
        return None

    df = pd.read_csv(schedule_path)
    completed = df[df["Status"] == "Completed"].copy()

    rows = []
    for team in sorted(completed["Team"].unique()):
        team_conference = team_conf.get(team)
        if team_conference is None:
            log.info("'%s' has no known conference for season %d yet, skipping.", team, season)
            continue

        tg = completed[completed["Team"] == team]
        conf_w = 0
        conf_l = 0
        for _, game in tg.iterrows():
            opponent = game["Opponent"]
            opponent_conference = team_conf.get(opponent)
            if opponent_conference is None or opponent_conference != team_conference:
                continue  # unclassifiable or genuinely non-conference
            if game["Outcome"] == "W":
                conf_w += 1
            elif game["Outcome"] == "L":
                conf_l += 1

        rows.append({
            "Season": season,
            "Team": team,
            "Conference": team_conference,
            "Conf_W": conf_w,
            "Conf_L": conf_l,
            "Conf_Record": f"{conf_w}-{conf_l}",
        })

    out_path = os.path.join(directory, f"conference_record_{season}.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    log.info("Wrote %d team(s) to %s.", len(rows), out_path)
    return len(rows)


def main():
    directory = "."
    season_arg = None
    for i, arg in enumerate(sys.argv):
        if arg == "--season" and i + 1 < len(sys.argv):
            season_arg = int(sys.argv[i + 1])

    if season_arg is not None:
        seasons = [season_arg]
    else:
        seasons = _seasons_with_schedule_data(directory)
        if not seasons:
            log.info("No dynasty_data_*.csv files found, nothing to compute.")
            return

    log.info("Computing conference records for season(s): %s", seasons)
    any_written = False
    for season in seasons:
        result = compute_for_season(season, directory)
        if result is not None:
            any_written = True

    if not any_written:
        log.info("No conference_record_*.csv files were written this run.")


if __name__ == "__main__":
    main()
