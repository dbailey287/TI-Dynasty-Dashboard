"""
Top 25 Weeks-Ranked Calculator
================================
Reads top25_rankings_history_<season>.csv (every poll ever appended by
schedule_scraper.py -- see append_top25_history() there) and counts, per
team per season, how many distinct polls that team appeared in the Top 25
at all, and how many of those were specifically in the Top 10.

Counts POLLS, not schedule weeks -- the Top 25 screenshot has no week
label of its own. One poll is expected roughly once a week (whenever a
new Top 25 screenshot is posted alongside that week's schedule
screenshots), so "weeks ranked" and "polls ranked" should track closely
in practice, but this is honestly a poll count, not a verified week count.

Season resolution: same pattern as compute_conference_record.py.
    --season CLI flag processes just that one season.
    Omitted -> recomputes EVERY season with a top25_rankings_history file,
    which keeps this self-healing if history ever needs correcting.

Output: top25_weeks_<season>.csv per season, columns
Season, Team, Polls_Ranked_Top25, Polls_Ranked_Top10, Total_Polls_This_Season.
Total_Polls_This_Season lets a caller compute "ranked X of Y polls so far"
rather than just a raw count with no denominator. Fully rebuilt each run.
"""

import csv
import glob
import logging
import os
import re
import sys

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("top25_weeks")

CSV_HEADER = ["Season", "Team", "Polls_Ranked_Top25", "Polls_Ranked_Top10", "Total_Polls_This_Season"]


def _seasons_with_history(directory: str = ".") -> list[int]:
    files = glob.glob(os.path.join(directory, "top25_rankings_history_*.csv"))
    seasons = []
    for path in files:
        m = re.search(r"top25_rankings_history_(\d+)\.csv$", os.path.basename(path))
        if m:
            seasons.append(int(m.group(1)))
    return sorted(seasons)


def compute_for_season(season: int, directory: str = ".") -> int | None:
    history_path = os.path.join(directory, f"top25_rankings_history_{season}.csv")
    if not os.path.exists(history_path):
        log.warning("No top25_rankings_history_%d.csv found, skipping season %d.", season, season)
        return None

    hist = pd.read_csv(history_path)
    if hist.empty:
        log.info("top25_rankings_history_%d.csv is empty, nothing to compute.", season)
        return None

    total_polls = hist["Poll_Number"].nunique()

    rows = []
    for team in sorted(hist["Team"].unique()):
        team_polls = hist[hist["Team"] == team]
        polls_top25 = team_polls["Poll_Number"].nunique()
        polls_top10 = team_polls[team_polls["Rank"] <= 10]["Poll_Number"].nunique()
        rows.append({
            "Season": season,
            "Team": team,
            "Polls_Ranked_Top25": polls_top25,
            "Polls_Ranked_Top10": polls_top10,
            "Total_Polls_This_Season": total_polls,
        })

    out_path = os.path.join(directory, f"top25_weeks_{season}.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    log.info("Wrote %d team(s) to %s (%d total poll(s) this season).", len(rows), out_path, total_polls)
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
        seasons = _seasons_with_history(directory)
        if not seasons:
            log.info("No top25_rankings_history_*.csv files found, nothing to compute.")
            return

    log.info("Computing Top 25 weeks-ranked for season(s): %s", seasons)
    for season in seasons:
        compute_for_season(season, directory)


if __name__ == "__main__":
    main()
