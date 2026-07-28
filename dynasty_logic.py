"""
Core data-loading, cleaning, and stats-computation logic for the
CFB Dynasty Command Center dashboard.

Kept separate from the Streamlit UI so the analytics can be tested
and reasoned about independently.
"""
import io
import re
from datetime import datetime

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WEEK_ORDER_OVERRIDE = {"Conf Champ": 900}  # sorts after all numbered weeks
CPU_LABEL = "CPU"

# Proxy "quality" win% used for CPU opponents when estimating strength of
# schedule, since CPU teams don't have their own tracked game logs in this
# export. Ranked CPU opponents get a quality score based on their AP rank
# tier; unranked CPU opponents get a flat baseline.
CPU_RANK_TIER_QUALITY = [
    (5, 0.88),
    (10, 0.80),
    (15, 0.74),
    (25, 0.68),
]
CPU_UNRANKED_QUALITY = 0.45


# ---------------------------------------------------------------------------
# Loading & cleaning
# ---------------------------------------------------------------------------

def _repair_raw_csv_text(raw_text: str) -> str:
    """
    Fixes a known export glitch where a small number of rows have the
    season value duplicated at the start of the line (e.g. '2026,2026,...'),
    which breaks column alignment. Safe no-op for rows that don't have it.
    """
    fixed_lines = []
    lines = raw_text.splitlines(keepends=True)
    if not lines:
        return raw_text
    header = lines[0]
    fixed_lines.append(header)
    for line in lines[1:]:
        fixed_lines.append(re.sub(r"^(\d{4}),\1,", r"\1,", line))
    return "".join(fixed_lines)


def load_raw_dataframe(file_like_or_path) -> pd.DataFrame:
    """Load the dynasty export CSV, repairing known formatting glitches."""
    if hasattr(file_like_or_path, "read"):
        raw_text = file_like_or_path.read()
        if isinstance(raw_text, bytes):
            raw_text = raw_text.decode("utf-8", errors="replace")
    else:
        with open(file_like_or_path, "r", encoding="utf-8", errors="replace") as f:
            raw_text = f.read()

    repaired = _repair_raw_csv_text(raw_text)
    df = pd.read_csv(io.StringIO(repaired), engine="python", on_bad_lines="skip")
    df.columns = [c.strip() for c in df.columns]
    return df


def week_sort_key(week_value) -> int:
    week_str = str(week_value).strip()
    if week_str in WEEK_ORDER_OVERRIDE:
        return WEEK_ORDER_OVERRIDE[week_str]
    try:
        return int(week_str)
    except ValueError:
        return 999


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Type-cleans columns and standardizes string fields."""
    df = df.copy()

    str_cols = [
        "Team", "User", "Location", "Opponent_Rank", "Opponent",
        "Opponent_User", "Matchup_Type", "Status", "Outcome", "Week",
    ]
    for col in str_cols:
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    df["Season"] = pd.to_numeric(df["Season"], errors="coerce").astype("Int64")

    for col in ["Team_Score", "Opponent_Score", "Margin"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["Opponent_Rank_Num"] = pd.to_numeric(df["Opponent_Rank"], errors="coerce")

    # Opponent_Rank_At_Game may not exist in CSVs produced before this
    # feature was added (older seasons) -- treat it as entirely unknown
    # rather than erroring, so old data still loads (just without the
    # frozen-rank view available).
    if "Opponent_Rank_At_Game" in df.columns:
        df["Opponent_Rank_At_Game_Num"] = pd.to_numeric(df["Opponent_Rank_At_Game"], errors="coerce")
    else:
        df["Opponent_Rank_At_Game"] = pd.NA
        df["Opponent_Rank_At_Game_Num"] = pd.NA

    df["Week_Sort"] = df["Week"].apply(week_sort_key)

    # Parse date (best effort; some rows have no date e.g. BYE weeks)
    def _parse_date(d):
        try:
            return pd.to_datetime(d, format="%a, %b %d", errors="coerce")
        except Exception:
            return pd.NaT
    df["Date_Parsed"] = df["Date"].apply(_parse_date)

    return df


def add_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Adds the boolean/derived columns called out in the design notes."""
    df = df.copy()

    df["Completed"] = df["Status"] == "Completed"
    df["Is_Bye"] = df["Status"] == "BYE"
    df["Ranked_Game"] = df["Opponent_Rank_Num"].notna()
    df["Top10_Game"] = df["Opponent_Rank_Num"] <= 10
    df["Home_Game"] = df["Location"] == "Home"
    df["Away_Game"] = df["Location"] == "Away"
    df["Win"] = df["Completed"] & (df["Outcome"] == "W")
    df["Loss"] = df["Completed"] & (df["Outcome"] == "L")
    df["Ranked_Win"] = df["Win"] & df["Ranked_Game"]
    df["Ranked_Loss"] = df["Loss"] & df["Ranked_Game"]
    df["Opponent_Is_User"] = df["Opponent_User"] != CPU_LABEL
    df["One_Score_Game"] = df["Completed"] & (df["Margin"].abs() <= 8)
    df["Blowout_Game"] = df["Completed"] & (df["Margin"].abs() >= 21)

    # "At-game" variants: same concepts, but based on the opponent's rank
    # FROZEN at the moment the game was actually played (see
    # Opponent_Rank_At_Game_Num), rather than their current/live rank. A
    # win over the #1 team stays a win over the #1 team even after that
    # team later slips in the rankings.
    df["Ranked_Game_AtGame"] = df["Opponent_Rank_At_Game_Num"].notna()
    df["Top10_Game_AtGame"] = df["Opponent_Rank_At_Game_Num"] <= 10
    df["Ranked_Win_AtGame"] = df["Win"] & df["Ranked_Game_AtGame"]
    df["Ranked_Loss_AtGame"] = df["Loss"] & df["Ranked_Game_AtGame"]

    # What to actually SHOW in a "Opp Rank" column: the frozen at-game rank
    # once it's been recorded (this includes the literal value "-" for a
    # game where the opponent was genuinely unranked at kickoff -- that's
    # real recorded data, not a missing value, so it must NOT get
    # overwritten by the live rank). Only fall back to the live rank when
    # no frozen value has been recorded yet at all (the game hasn't been
    # played, or this row predates the rank-freezing feature).
    df["Opponent_Rank_Display"] = df["Opponent_Rank_At_Game"].fillna(df["Opponent_Rank"])
    df["Opponent_Rank_Display_Num"] = pd.to_numeric(df["Opponent_Rank_Display"], errors="coerce")

    # Game_Id: unique per actual game (User-vs-User games appear twice in
    # the export -- once per team's perspective -- and need to collapse to
    # one row for league-wide, non-double-counted stats).
    def _game_id(row):
        if row["Opponent_Is_User"]:
            teams = sorted([row["Team"], row["Opponent"]])
            return f"{teams[0]}__{teams[1]}__W{row['Week']}__{row['Season']}"
        return f"{row['Team']}__{row['Opponent']}__W{row['Week']}__{row['Season']}__cpu"
    df["Game_Id"] = df.apply(_game_id, axis=1)

    return df


def load_and_prepare(file_like_or_path) -> pd.DataFrame:
    raw = load_raw_dataframe(file_like_or_path)
    cleaned = clean_dataframe(raw)
    derived = add_derived_columns(cleaned)
    return derived


def default_current_week_sort(df: pd.DataFrame):
    """
    Auto-detects the 'active' week: the earliest week that still has at
    least one game with Status == 'Upcoming'. Returns None if every game
    in the export has already been played (or there's no data).
    """
    upcoming = df[df["Status"] == "Upcoming"]
    if upcoming.empty:
        return None
    return int(upcoming["Week_Sort"].min())


def load_roster_entries(directory: str = ".") -> list:
    """Returns the full active roster (team/username/display_name/user_id)
    via roster.py, or [] if roster.py / the CSV aren't present."""
    try:
        import roster as _roster
    except ImportError:
        return []
    path = _roster.find_roster_csv(directory)
    if not path:
        return []
    try:
        return _roster.load_roster(path)
    except (OSError, KeyError):
        return []


def load_roster_display_names(directory: str = ".") -> dict:
    """Returns {team: display_name} from Server_Members_Teams.csv, or {}
    if roster.py / the CSV aren't present -- the dashboard falls back to
    whatever the scraper originally wrote in that case, rather than
    crashing over a missing optional file."""
    try:
        import roster as _roster
    except ImportError:
        return {}
    path = _roster.find_roster_csv(directory)
    if not path:
        return {}
    try:
        entries = _roster.load_roster(path)
    except (OSError, KeyError):
        return {}
    return _roster.team_to_display_name(entries)


def apply_display_names(df: pd.DataFrame, team_display_map: dict) -> pd.DataFrame:
    """Overrides the User column with the roster's current display_name,
    for any team present in that mapping. Teams not in the roster (e.g.
    older seasons, or a team not yet added to the CSV) keep whatever the
    scraper originally wrote -- this never removes information, only
    upgrades it when a better source is available."""
    if not team_display_map:
        return df
    df = df.copy()
    mask = df["Team"].isin(team_display_map)
    df.loc[mask, "User"] = df.loc[mask, "Team"].map(team_display_map)
    return df


def rta_status_diagnostic(path: str = "rta_status.json") -> str | None:
    """
    Returns None if there's nothing to report -- the file is missing
    entirely (e.g. the RTA automation just isn't set up yet) or it loaded
    fine. Returns a short human-readable reason if the file EXISTS but
    couldn't be parsed (e.g. leftover git merge-conflict markers from a
    conflict that wasn't fully resolved), so the dashboard can show a
    clear message instead of silently hiding the section either way.
    """
    import json
    import os
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            json.load(f)
        return None
    except json.JSONDecodeError as e:
        return (
            f"rta_status.json exists but isn't valid JSON ({e}). Check for "
            "leftover git merge-conflict markers (<<<<<<<, =======, >>>>>>>)."
        )
    except OSError as e:
        return f"rta_status.json exists but couldn't be read: {e}"


def load_rta_status(path: str = "rta_status.json") -> dict | None:
    """
    Reads the Ready-To-Advance status file written by the RTA tracker's
    GitHub Actions job. Returns None if the file doesn't exist (e.g. the
    tracker isn't set up yet) rather than raising, so the dashboard can
    just hide the section gracefully.
    """
    import json
    import os
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def has_at_game_rank_data(df: pd.DataFrame) -> bool:
    """
    True if this data has usable Opponent_Rank_At_Game values (i.e. was
    scraped with the version of the scraper that tracks frozen ranks).
    Older seasons scraped before that feature existed won't have it --
    the dashboard falls back to live-rank rankings for those rather than
    showing an all-empty "at time of game" view.
    """
    completed = df[df["Completed"]]
    if completed.empty or "Ranked_Game_AtGame" not in df.columns:
        return False
    return bool(completed["Ranked_Game_AtGame"].any())


def get_unique_games(df: pd.DataFrame, completed_only: bool = True) -> pd.DataFrame:
    """One row per actual game (dedupes User-vs-User games)."""
    subset = df[df["Completed"]] if completed_only else df
    return subset.drop_duplicates(subset="Game_Id", keep="first")


# ---------------------------------------------------------------------------
# Team season stats
# ---------------------------------------------------------------------------

def _streak_and_form(team_games: pd.DataFrame, form_window: int = 5):
    """team_games must already be sorted chronologically (completed only)."""
    if team_games.empty:
        return "-", 0.0
    outcomes = team_games["Outcome"].tolist()
    # current streak
    last = outcomes[-1]
    streak_len = 0
    for o in reversed(outcomes):
        if o == last:
            streak_len += 1
        else:
            break
    streak_label = f"{last}{streak_len}" if last in ("W", "L") else "-"

    recent = outcomes[-form_window:]
    form_pct = (recent.count("W") / len(recent)) if recent else 0.0
    return streak_label, form_pct


def _longest_streaks(team_games: pd.DataFrame) -> tuple:
    """team_games must already be sorted chronologically (completed only).
    Returns (longest_win_streak, longest_loss_streak) for the WHOLE season,
    not just the current one."""
    if team_games.empty:
        return 0, 0
    longest_w = longest_l = cur_w = cur_l = 0
    for o in team_games["Outcome"]:
        if o == "W":
            cur_w += 1
            cur_l = 0
        elif o == "L":
            cur_l += 1
            cur_w = 0
        longest_w = max(longest_w, cur_w)
        longest_l = max(longest_l, cur_l)
    return longest_w, longest_l


def compute_split_stats(df: pd.DataFrame, teams: list, rank_basis: str = "at_game") -> pd.DataFrame:
    """
    Builds the "advanced splits" table for the League Stats page: scoring
    broken out by win/loss, home/away, User-vs-CPU, and ranked/unranked
    opponent quality, plus blowout/nail-biter rates and current streak.
    Complements compute_league_stats_table rather than duplicating it --
    that one is season totals/bests; this one is about how a team's
    performance splits across different contexts.
    """
    rank_col = "Opponent_Rank_At_Game_Num" if rank_basis == "at_game" else "Opponent_Rank_Num"
    completed = df[df["Completed"]].copy()
    rows = []
    for team in teams:
        tg = completed[completed["Team"] == team]
        if tg.empty:
            rows.append({"Team": team})
            continue

        wins = tg[tg["Outcome"] == "W"]
        losses = tg[tg["Outcome"] == "L"]
        home = tg[tg["Location"] == "Home"]
        away = tg[tg["Location"] == "Away"]
        user_games = tg[tg["Opponent_Is_User"]]
        cpu_games = tg[~tg["Opponent_Is_User"]]
        ranked = tg[tg[rank_col].notna()]
        unranked = tg[tg[rank_col].isna()]

        def _avg(subset, col):
            return subset[col].mean() if len(subset) else np.nan

        gp = len(tg)
        rows.append({
            "Team": team,
            "Margin_Wins": _avg(wins, "Margin"),
            "Margin_Losses": _avg(losses, "Margin"),
            "Home_PPG": _avg(home, "Team_Score"), "Home_PA": _avg(home, "Opponent_Score"), "Home_Margin": _avg(home, "Margin"),
            "Away_PPG": _avg(away, "Team_Score"), "Away_PA": _avg(away, "Opponent_Score"), "Away_Margin": _avg(away, "Margin"),
            "User_PPG": _avg(user_games, "Team_Score"), "User_PA": _avg(user_games, "Opponent_Score"), "User_Margin": _avg(user_games, "Margin"),
            "CPU_PPG": _avg(cpu_games, "Team_Score"), "CPU_PA": _avg(cpu_games, "Opponent_Score"), "CPU_Margin": _avg(cpu_games, "Margin"),
            "Margin_vs_Ranked": _avg(ranked, "Margin"),
            "Margin_vs_Unranked": _avg(unranked, "Margin"),
            "Blowout_Rate": (tg["Blowout_Game"].sum() / gp * 100) if gp else np.nan,
            "OneScore_Rate": (tg["One_Score_Game"].sum() / gp * 100) if gp else np.nan,
        })

    result = pd.DataFrame(rows).set_index("Team")
    return result.reindex(teams)


def signature_win(df: pd.DataFrame, rank_basis: str = "at_game"):
    """League-wide: the single win over the highest-ranked (lowest rank
    number) opponent all season -- distinct from biggest_blowout, which
    only considers margin and ignores opponent quality entirely."""
    rank_col = "Opponent_Rank_At_Game_Num" if rank_basis == "at_game" else "Opponent_Rank_Num"
    completed = df[df["Completed"]]
    wins = completed[(completed["Outcome"] == "W") & completed[rank_col].notna()]
    if wins.empty:
        return None
    idx = wins[rank_col].idxmin()
    row = wins.loc[idx]
    return {
        "team": row["Team"], "opponent": row["Opponent"], "opponent_rank": int(row[rank_col]),
        "score": f"{row['Team_Score']:.0f}-{row['Opponent_Score']:.0f}",
    }


def worst_loss(df: pd.DataFrame, rank_basis: str = "at_game"):
    """League-wide: the biggest-margin loss to an UNRANKED opponent --
    distinct from biggest_blowout, which could be any big-margin game
    regardless of who lost or the opponent's quality."""
    rank_col = "Opponent_Rank_At_Game_Num" if rank_basis == "at_game" else "Opponent_Rank_Num"
    completed = df[df["Completed"]]
    bad_losses = completed[(completed["Outcome"] == "L") & completed[rank_col].isna()]
    if bad_losses.empty:
        return None
    idx = bad_losses["Margin"].idxmin()  # Margin is negative for a loss; most negative = worst
    row = bad_losses.loc[idx]
    return {
        "team": row["Team"], "opponent": row["Opponent"],
        "score": f"{row['Opponent_Score']:.0f}-{row['Team_Score']:.0f}",
        "margin": abs(row["Margin"]),
    }


def compute_league_stats_table(df: pd.DataFrame, teams: list, team_stats: pd.DataFrame) -> pd.DataFrame:
    """
    Builds one comprehensive row per team for the League Stats page,
    combining what's already in team_stats (PPG, points allowed, average
    margin, records, SOS, etc.) with a handful of new per-team stats
    computed fresh from the game log: total points, best/worst single-game
    performances, one-score game record, longest win/loss streaks, and a
    "win quality" score (reusing the same rank-weighted scoring the
    Dynasty Rating formula uses -- see compute_dynasty_rating).
    """
    completed = df[df["Completed"]].copy()
    rows = []
    for team in teams:
        tg = completed[completed["Team"] == team].sort_values("Week_Sort")
        if tg.empty:
            rows.append({"Team": team})
            continue

        wins = tg[tg["Outcome"] == "W"]
        losses = tg[tg["Outcome"] == "L"]
        one_score = tg[tg["One_Score_Game"]]
        longest_w, longest_l = _longest_streaks(tg)

        rank_col = "Opponent_Rank_At_Game_Num"
        ranked_wins = wins[wins[rank_col].notna()] if rank_col in wins.columns else wins.iloc[0:0]
        win_quality = float((26 - pd.to_numeric(ranked_wins[rank_col], errors="coerce")).clip(lower=0).sum()) if len(ranked_wins) else 0.0

        rows.append({
            "Team": team,
            "Total_PF": int(tg["Team_Score"].sum()),
            "Total_PA": int(tg["Opponent_Score"].sum()),
            "Best_Game_PF": int(tg["Team_Score"].max()),
            "Worst_Game_PA": int(tg["Opponent_Score"].max()),
            "Biggest_Win_Margin": int(wins["Margin"].max()) if len(wins) else None,
            "Worst_Loss_Margin": int(losses["Margin"].min()) if len(losses) else None,
            "One_Score_W": int((one_score["Outcome"] == "W").sum()),
            "One_Score_L": int((one_score["Outcome"] == "L").sum()),
            "Longest_Win_Streak": longest_w,
            "Longest_Loss_Streak": longest_l,
            "Win_Quality_Score": round(win_quality, 1),
        })

    extra = pd.DataFrame(rows).set_index("Team")
    return team_stats.join(extra, how="left")


def compute_team_stats(df: pd.DataFrame, teams: list, as_of_week_sort: int | None = None,
                        rank_basis: str = "at_game") -> pd.DataFrame:
    """
    Builds one row per team with season-to-date stats.
    If as_of_week_sort is given, only games with Week_Sort <= that value
    are considered (used to compute "last week's" snapshot for trend arrows).

    rank_basis controls which opponent-rank concept "Ranked_W"/"Top10_W"/etc.
    are built from:
      - "at_game" (default): the opponent's rank FROZEN at the moment the
        game was played -- a win over the #1 team stays a win over the #1
        team even after they later slip in the rankings.
      - "live": the opponent's CURRENT rank as of the most recent scrape,
        which can retroactively make a past win look weaker (or stronger)
        as that opponent's own record changes.
    """
    ranked_game_col = "Ranked_Game_AtGame" if rank_basis == "at_game" else "Ranked_Game"
    top10_game_col = "Top10_Game_AtGame" if rank_basis == "at_game" else "Top10_Game"

    rows = []
    completed = df[df["Completed"]].copy()
    if as_of_week_sort is not None:
        completed = completed[completed["Week_Sort"] <= as_of_week_sort]

    for team in teams:
        tg = completed[completed["Team"] == team].sort_values("Week_Sort")
        user = df.loc[df["Team"] == team, "User"].iloc[0] if (df["Team"] == team).any() else ""

        games_played = len(tg)
        wins = int((tg["Outcome"] == "W").sum())
        losses = int((tg["Outcome"] == "L").sum())

        home = tg[tg["Location"] == "Home"]
        away = tg[tg["Location"] == "Away"]
        vs_user = tg[tg["Opponent_Is_User"]]
        vs_cpu = tg[~tg["Opponent_Is_User"]]
        ranked = tg[tg[ranked_game_col]]
        unranked = tg[~tg[ranked_game_col]]
        top10 = tg[tg[top10_game_col]]

        pf = tg["Team_Score"].mean() if games_played else np.nan
        pa = tg["Opponent_Score"].mean() if games_played else np.nan
        mov = tg["Margin"].mean() if games_played else np.nan

        streak_label, form_pct = _streak_and_form(tg)
        rank_num_col = "Opponent_Rank_At_Game_Num" if rank_basis == "at_game" else "Opponent_Rank_Num"

        def _win_count(subset, location=None, min_margin=None):
            s = subset[subset["Outcome"] == "W"]
            if location is not None:
                s = s[s["Location"] == location]
            if min_margin is not None:
                s = s[s["Margin"] >= min_margin]
            return int(len(s))

        def _win_quality_sum(subset, location=None, min_margin=None):
            """Sums (26 - opponent_rank) per qualifying win, so beating a
            higher-ranked (lower-numbered) opponent contributes more than
            beating a lower-ranked one -- a win over #3 contributes 23
            points, a win over #24 only 2. Fed into the rating formula;
            the plain _win_count columns are kept separately so the "Why
            this ranking?" display still shows an honest game count."""
            s = subset[subset["Outcome"] == "W"]
            if location is not None:
                s = s[s["Location"] == location]
            if min_margin is not None:
                s = s[s["Margin"] >= min_margin]
            ranks = pd.to_numeric(s[rank_num_col], errors="coerce")
            quality = (26 - ranks).clip(lower=0)
            return float(quality.sum())

        rows.append({
            "Team": team,
            "User": user,
            "GP": games_played,
            "W": wins,
            "L": losses,
            "Win_Pct": (wins / games_played) if games_played else np.nan,
            "Home_W": int((home["Outcome"] == "W").sum()),
            "Home_L": int((home["Outcome"] == "L").sum()),
            "Away_W": int((away["Outcome"] == "W").sum()),
            "Away_L": int((away["Outcome"] == "L").sum()),
            "User_W": int((vs_user["Outcome"] == "W").sum()),
            "User_L": int((vs_user["Outcome"] == "L").sum()),
            "CPU_W": int((vs_cpu["Outcome"] == "W").sum()),
            "CPU_L": int((vs_cpu["Outcome"] == "L").sum()),
            "Ranked_W": int((ranked["Outcome"] == "W").sum()),
            "Ranked_L": int((ranked["Outcome"] == "L").sum()),
            "Top10_W": int((top10["Outcome"] == "W").sum()),
            "Top10_L": int((top10["Outcome"] == "L").sum()),
            # Granular win-quality categories, used by the Dynasty Rating formula --
            # see DEFAULT_RATING_WEIGHTS. "Big" margins: 17+ vs ranked opponents,
            # 28+ vs unranked opponents. Plain counts (for display) plus
            # rank-quality-weighted sums (what the rating actually uses for
            # the 4 ranked-win categories -- see _win_quality_sum above).
            "Road_Ranked_W": _win_count(ranked, location="Away"),
            "Home_Ranked_W": _win_count(ranked, location="Home"),
            "Road_Ranked_W_Big": _win_count(ranked, location="Away", min_margin=17),
            "Home_Ranked_W_Big": _win_count(ranked, location="Home", min_margin=17),
            "Road_Ranked_W_Quality": _win_quality_sum(ranked, location="Away"),
            "Home_Ranked_W_Quality": _win_quality_sum(ranked, location="Home"),
            "Road_Ranked_W_Big_Quality": _win_quality_sum(ranked, location="Away", min_margin=17),
            "Home_Ranked_W_Big_Quality": _win_quality_sum(ranked, location="Home", min_margin=17),
            "Road_Unranked_W": _win_count(unranked, location="Away"),
            "Home_Unranked_W": _win_count(unranked, location="Home"),
            "Road_Unranked_W_Big": _win_count(unranked, location="Away", min_margin=28),
            "Home_Unranked_W_Big": _win_count(unranked, location="Home", min_margin=28),
            "PF": pf,
            "PA": pa,
            "MOV": mov,
            "Streak": streak_label,
            "Form_Pct": form_pct,
            "One_Score_Games": int(tg["One_Score_Game"].sum()),
            "Blowout_Wins": int(((tg["Outcome"] == "W") & tg["Blowout_Game"]).sum()),
        })

    stats = pd.DataFrame(rows).set_index("Team")
    return stats


def _cpu_opponent_quality(rank_num) -> float:
    if pd.isna(rank_num):
        return CPU_UNRANKED_QUALITY
    for threshold, quality in CPU_RANK_TIER_QUALITY:
        if rank_num <= threshold:
            return quality
    return 0.60  # ranked but outside top 25 tiers listed (shouldn't happen)


def add_strength_of_schedule(df: pd.DataFrame, team_stats: pd.DataFrame,
                              as_of_week_sort: int | None = None,
                              rank_basis: str = "at_game") -> pd.DataFrame:
    """
    Two-pass SOS: for opponents that are one of our tracked teams, use that
    opponent's own win% (already computed in team_stats). For CPU opponents,
    use a rank-tier quality proxy -- based on that opponent's rank AT THE
    TIME of the game (rank_basis="at_game", default) or their current rank
    (rank_basis="live"). Adds an 'SOS' column (0-1 scale, higher = tougher)
    to a copy of team_stats.
    """
    rank_col = "Opponent_Rank_At_Game_Num" if rank_basis == "at_game" else "Opponent_Rank_Num"

    stats = team_stats.copy()
    completed = df[df["Completed"]].copy()
    if as_of_week_sort is not None:
        completed = completed[completed["Week_Sort"] <= as_of_week_sort]

    sos_values = {}
    for team in stats.index:
        tg = completed[completed["Team"] == team]
        if tg.empty:
            sos_values[team] = np.nan
            continue
        qualities = []
        for _, row in tg.iterrows():
            if row["Opponent_Is_User"] and row["Opponent"] in stats.index:
                opp_win_pct = stats.loc[row["Opponent"], "Win_Pct"]
                qualities.append(opp_win_pct if pd.notna(opp_win_pct) else 0.5)
            else:
                qualities.append(_cpu_opponent_quality(row[rank_col]))
        sos_values[team] = float(np.mean(qualities)) if qualities else np.nan

    stats["SOS"] = pd.Series(sos_values)
    return stats


# ---------------------------------------------------------------------------
# Dynasty Rating (power rankings)
# ---------------------------------------------------------------------------

def compute_rating_history(df: pd.DataFrame, teams: list, weights: dict = None,
                            rank_basis: str = "at_game") -> pd.DataFrame:
    """
    Recomputes the Dynasty Rating as of every completed week (using the
    same as_of_week_sort mechanism used for trend arrows), producing a
    full season trajectory per team. No snapshot persistence needed since
    ratings are fully re-derivable from the game log at any point in time.
    Returns columns: Week_Sort, Week, Team, User, Dynasty_Rating, Rank.
    """
    completed_weeks = sorted(df.loc[df["Completed"], "Week_Sort"].unique())
    week_label_map = {w: df.loc[df["Week_Sort"] == w, "Week"].iloc[0] for w in completed_weeks}

    rows = []
    for w in completed_weeks:
        stats = compute_team_stats(df, teams, as_of_week_sort=w, rank_basis=rank_basis)
        stats = add_strength_of_schedule(df, stats, as_of_week_sort=w, rank_basis=rank_basis)
        week_rated = compute_dynasty_rating(stats, weights)
        for team, row in week_rated.iterrows():
            rows.append({
                "Week_Sort": w,
                "Week": week_label_map[w],
                "Team": team,
                "User": row["User"],
                "Dynasty_Rating": row["Dynasty_Rating"],
                "Rank": row["Rank"],
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Shareable recap text
# ---------------------------------------------------------------------------

def format_weekly_recap_text(recap: dict, week_label) -> str:
    """Formats the weekly_recap() dict into a copy-paste-ready text blurb
    (Discord/group-chat friendly markdown)."""
    if not recap:
        return ""

    lines = [f"🏈 **WEEK {week_label} RECAP** 🏈", ""]

    g = recap.get("game_of_week")
    if g:
        lines += ["🎮 **Game of the Week**", f"{g['winner']} def. {g['loser']}, {g['score']}", ""]

    if recap.get("upset"):
        lines += ["😱 **Upset Alert**", recap["upset"], ""]

    hs = recap.get("highest_scoring")
    if hs:
        lines += [
            "🔢 **Highest Scoring**",
            f"{hs['team_a']} {hs['score_a']:.0f} - {hs['score_b']:.0f} {hs['team_b']}",
            "",
        ]

    bd = recap.get("best_defense")
    if bd:
        lines += [
            "🛡️ **Defensive Performance**",
            f"{bd['team']} held {bd['opponent']} to {bd['points_allowed']:.0f} points",
            "",
        ]

    return "\n".join(lines).strip()


def compute_multi_season_rating_history(df_all: pd.DataFrame, weights: dict = None,
                                         rank_basis: str = "at_game") -> pd.DataFrame:
    """
    Stitches together a season-by-season Dynasty Rating history across every
    season present in df_all into one continuous timeline. Ratings reset at
    the start of each season (computed fresh from that season's game log
    only) since that matches the game itself -- everyone starts 0-0 at
    kickoff each year.
    """
    frames = []
    seasons = sorted(df_all["Season"].dropna().unique())
    for season in seasons:
        season_df = df_all[df_all["Season"] == season]
        season_teams = sorted(season_df["Team"].unique())
        if not season_teams:
            continue
        hist = compute_rating_history(season_df, season_teams, weights, rank_basis=rank_basis)
        if hist.empty:
            continue
        hist = hist.copy()
        hist["Season"] = season
        frames.append(hist)

    cols = ["Season", "Week_Sort", "Week", "Team", "User", "Dynasty_Rating", "Rank",
            "Global_Order", "Season_Week_Label"]
    if not frames:
        return pd.DataFrame(columns=cols)

    combined = pd.concat(frames, ignore_index=True)
    combined["Global_Order"] = combined["Season"].astype(int) * 10000 + combined["Week_Sort"]
    combined["Season_Week_Label"] = combined["Season"].astype(int).astype(str) + " Wk" + combined["Week"].astype(str)
    combined = combined.sort_values("Global_Order").reset_index(drop=True)
    return combined[cols]


def compute_career_stats(df_all: pd.DataFrame, rating_history: pd.DataFrame = None) -> pd.DataFrame:
    """
    Aggregates stats per User (coach) across every season in df_all, since
    the team a person controls can change from year to year. Optionally
    joins in each user's best season (by final Dynasty Rating that season)
    if a multi-season rating_history is supplied.
    """
    completed = df_all[df_all["Completed"]]
    users = sorted(u for u in completed["User"].dropna().unique() if u and u != "nan")

    rows = []
    for user in users:
        ug = completed[completed["User"] == user]
        seasons = sorted(ug["Season"].dropna().unique().tolist())

        teams_by_season = (
            ug.drop_duplicates(["Season", "Team"])
            .groupby("Season")["Team"]
            .apply(lambda s: ", ".join(sorted(s.unique())))
            .to_dict()
        )
        teams_str = "; ".join(f"{int(s)}: {t}" for s, t in sorted(teams_by_season.items()))

        wins = int((ug["Outcome"] == "W").sum())
        losses = int((ug["Outcome"] == "L").sum())
        ranked_wins = int(((ug["Outcome"] == "W") & ug["Ranked_Game"]).sum())
        uu = ug[ug["Opponent_Is_User"]]
        uu_w = int((uu["Outcome"] == "W").sum())
        uu_l = int((uu["Outcome"] == "L").sum())

        rows.append({
            "User": user,
            "Seasons_Played": len(seasons),
            "Teams_By_Season": teams_str,
            "Career_W": wins,
            "Career_L": losses,
            "Win_Pct": (wins / (wins + losses)) if (wins + losses) else np.nan,
            "Ranked_Wins": ranked_wins,
            "UU_W": uu_w,
            "UU_L": uu_l,
        })

    base_cols = ["Seasons_Played", "Teams_By_Season", "Career_W", "Career_L",
                 "Win_Pct", "Ranked_Wins", "UU_W", "UU_L"]
    stats = pd.DataFrame(rows).set_index("User") if rows else pd.DataFrame(columns=base_cols)

    if rating_history is not None and not rating_history.empty and not stats.empty:
        idx = rating_history.groupby(["User", "Season"])["Global_Order"].idxmax()
        season_finals = rating_history.loc[idx]
        best_idx = season_finals.groupby("User")["Dynasty_Rating"].idxmax()
        best = season_finals.loc[best_idx].set_index("User")[["Season", "Team", "Dynasty_Rating"]]
        best = best.rename(columns={
            "Season": "Best_Season", "Team": "Best_Season_Team", "Dynasty_Rating": "Best_Season_Rating",
        })
        stats = stats.join(best, how="left")

    return stats.sort_values("Win_Pct", ascending=False)


# ---------------------------------------------------------------------------
# Team branding: logos (ESPN's public CDN) and school colors
# ---------------------------------------------------------------------------
# Logo URLs point at ESPN's public team-logo CDN (a.espncdn.com) rather than
# bundling image files -- nothing is hosted or redistributed here, the app
# just links to ESPN's own hosted asset at render time. IDs were confirmed
# against ESPN's live team API (site.api.espn.com / site.web.api.espn.com)
# as of July 2026. This intentionally covers the full FBS, not just your
# 17 league teams -- CPU opponents (Ohio State, Georgia, Utah, etc.) need
# logos too, or the Schedule/Teams/League Stats pages look inconsistent
# with some rows having a logo and others not.
#
# If a logo ever fails to render for a team not in this dict yet: search
# "espn <team> football" -> the team page URL ends in /id/<NUMBER>/... ->
# add that ID here.
TEAM_ESPN_ID = {
    # --- Your 17 league teams ---
    "Arizona State": "9", "Arkansas": "8", "Baylor": "239", "California": "25",
    "Colorado": "38", "Missouri": "142", "Northwestern": "77", "Oklahoma State": "197",
    "Pittsburgh": "221", "SMU": "2567", "South Carolina": "2579", "Stanford": "24",
    "Temple": "218", "Virginia": "258", "Virginia Tech": "259", "West Virginia": "277",
    "Wisconsin": "275",

    # --- ACC ---
    "Boston College": "103", "Clemson": "228", "Duke": "150", "Florida State": "52",
    "Georgia Tech": "59", "Louisville": "97", "Miami": "2390", "NC State": "152",
    "North Carolina": "153", "Syracuse": "183", "Wake Forest": "154",

    # --- American ---
    "Army": "349", "Charlotte": "2429", "East Carolina": "151", "Florida Atlantic": "2226",
    "Memphis": "235", "Navy": "2426", "North Texas": "249", "Rice": "242",
    "South Florida": "58", "Tulane": "2655", "Tulsa": "202", "UAB": "5",
    "UTSA": "2636",

    # --- SEC ---
    "Alabama": "333", "Auburn": "2", "Florida": "57", "Georgia": "61",
    "Kentucky": "96", "LSU": "99", "Mississippi State": "344", "Ole Miss": "145",
    "Oklahoma": "201", "Tennessee": "2633", "Texas": "251", "Texas A&M": "245",
    "Vanderbilt": "238",

    # --- Big Ten ---
    "Illinois": "356", "Indiana": "84", "Iowa": "2294", "Maryland": "120",
    "Michigan": "130", "Michigan State": "127", "Minnesota": "135", "Nebraska": "158",
    "Ohio State": "194", "Oregon": "2483", "Penn State": "213", "Purdue": "2509",
    "Rutgers": "164", "UCLA": "26", "USC": "30", "Washington": "264",

    # --- Big 12 ---
    "Arizona": "12", "BYU": "252", "Cincinnati": "2132", "Houston": "248",
    "Iowa State": "66", "Kansas": "2305", "Kansas State": "2306", "TCU": "2628",
    "Texas Tech": "2641", "UCF": "2116", "Utah": "254",

    # --- Notable independents / other G5 ---
    "Notre Dame": "87", "Air Force": "2005", "Boise State": "68", "San Diego State": "21",
    "Fresno State": "278", "Wyoming": "2751", "Colorado State": "36",
    "Bowling Green": "189", "Toledo": "2649", "Ohio": "195", "Akron": "2006",
    "Kent State": "2309", "Miami (OH)": "193", "Buffalo": "2084",
    "Central Michigan": "2117", "Eastern Michigan": "2199", "Western Michigan": "2711",
    "Ball State": "2050",

    # --- Confirmed missing from user-reported screenshots (July 2026) ---
    "Washington State": "265", "Oregon State": "204", "UConn": "41",
    "UTEP": "2638", "Florida International": "2229",
}

# (primary, secondary) hex colors, no leading '#', official school colors.
TEAM_COLORS = {
    "Arizona State": ("8C1D40", "FFC627"), "Arkansas": ("9D2235", "FFFFFF"),
    "Baylor": ("154734", "FFB81C"), "California": ("003262", "FDB515"),
    "Colorado": ("CFB87C", "000000"), "Kentucky": ("0033A0", "FFFFFF"),
    "Missouri": ("F1B82D", "000000"),
    "Northwestern": ("4E2A84", "FFFFFF"), "Oklahoma State": ("FF7300", "000000"),
    "Pittsburgh": ("003594", "FFB81C"), "SMU": ("C8102E", "354CA1"),
    "South Carolina": ("73000A", "000000"), "Stanford": ("8C1515", "FFFFFF"),
    "Temple": ("9D2235", "FFFFFF"), "Virginia": ("232D4B", "F84C1E"),
    "Virginia Tech": ("630031", "CF4420"), "West Virginia": ("002855", "EAAA00"),
    "Wisconsin": ("C5050C", "FFFFFF"),
}
DEFAULT_TEAM_COLOR = ("2a2f3a", "888888")  # neutral fallback for an unrecognized team


def logo_url(team: str, dark_bg: bool = True) -> str | None:
    """ESPN CDN logo URL for a team, or None if not in TEAM_ESPN_ID.
    dark_bg=True uses the white-friendly variant (better on a dark theme)."""
    espn_id = TEAM_ESPN_ID.get(team)
    if not espn_id:
        return None
    variant = "500-dark" if dark_bg else "500"
    return f"https://a.espncdn.com/i/teamlogos/ncaa/{variant}/{espn_id}.png"


def team_primary_color(team: str) -> str:
    return "#" + TEAM_COLORS.get(team, DEFAULT_TEAM_COLOR)[0]


def team_secondary_color(team: str) -> str:
    return "#" + TEAM_COLORS.get(team, DEFAULT_TEAM_COLOR)[1]


DEFAULT_RATING_WEIGHTS = {
    "user_wins": 0.18,               # 1. User-vs-User game wins -- the "real" competition, still #1 but no longer overwhelming
    "road_ranked_wins": 0.16,       # 2. Wins over ranked opponents, on the road
    "home_ranked_wins": 0.13,       # 3. Wins over ranked opponents, at home
    "road_ranked_wins_big": 0.10,    # 4. ...by 17+ points
    "home_ranked_wins_big": 0.08,    # 5. ...by 17+ points
    "road_unranked_wins": 0.07,       # 6. Wins over unranked opponents, on the road
    "home_unranked_wins": 0.05,       # 7. Wins over unranked opponents, at home
    "road_unranked_wins_big": 0.04,    # 8. ...by 28+ points
    "home_unranked_wins_big": 0.03,    # 9. ...by 28+ points
    "win_pct": 0.16,                    # baseline: overall record now carries real weight, balances the user-win swing
}


def _minmax_scale(series: pd.Series) -> pd.Series:
    s = series.astype(float)
    if s.notna().sum() == 0:
        return s * 0
    lo, hi = s.min(skipna=True), s.max(skipna=True)
    if pd.isna(lo) or pd.isna(hi) or hi == lo:
        return s.apply(lambda v: 50.0 if pd.notna(v) else 0.0)
    return ((s - lo) / (hi - lo)) * 100.0


def compute_dynasty_rating(team_stats_with_sos: pd.DataFrame,
                            weights: dict = None) -> pd.DataFrame:
    weights = weights or DEFAULT_RATING_WEIGHTS
    stats = team_stats_with_sos.copy()

    win_pct_scaled = _minmax_scale(stats["Win_Pct"].fillna(0))
    user_wins_scaled = _minmax_scale(stats["User_W"].fillna(0))
    road_ranked_scaled = _minmax_scale(stats["Road_Ranked_W_Quality"].fillna(0))
    home_ranked_scaled = _minmax_scale(stats["Home_Ranked_W_Quality"].fillna(0))
    road_ranked_big_scaled = _minmax_scale(stats["Road_Ranked_W_Big_Quality"].fillna(0))
    home_ranked_big_scaled = _minmax_scale(stats["Home_Ranked_W_Big_Quality"].fillna(0))
    road_unranked_scaled = _minmax_scale(stats["Road_Unranked_W"].fillna(0))
    home_unranked_scaled = _minmax_scale(stats["Home_Unranked_W"].fillna(0))
    road_unranked_big_scaled = _minmax_scale(stats["Road_Unranked_W_Big"].fillna(0))
    home_unranked_big_scaled = _minmax_scale(stats["Home_Unranked_W_Big"].fillna(0))

    rating = (
        weights["user_wins"] * user_wins_scaled
        + weights["road_ranked_wins"] * road_ranked_scaled
        + weights["home_ranked_wins"] * home_ranked_scaled
        + weights["road_ranked_wins_big"] * road_ranked_big_scaled
        + weights["home_ranked_wins_big"] * home_ranked_big_scaled
        + weights["road_unranked_wins"] * road_unranked_scaled
        + weights["home_unranked_wins"] * home_unranked_scaled
        + weights["road_unranked_wins_big"] * road_unranked_big_scaled
        + weights["home_unranked_wins_big"] * home_unranked_big_scaled
        + weights["win_pct"] * win_pct_scaled
    )

    stats["Dynasty_Rating"] = rating.round(1)
    stats["Rating_Component_WinPct"] = win_pct_scaled
    stats["Rating_Component_UserWins"] = user_wins_scaled
    stats["Rating_Component_RoadRankedWins"] = road_ranked_scaled

    stats = stats.sort_values("Dynasty_Rating", ascending=False)
    stats["Rank"] = range(1, len(stats) + 1)
    return stats


def compute_rating_trend(df: pd.DataFrame, teams: list, weights: dict = None,
                          rank_basis: str = "at_game") -> pd.DataFrame:
    """
    Computes current Dynasty Rating and the rating/rank as of one week
    earlier, returning current ranking with a Rank_Change column
    (positive = moved up).
    """
    completed_weeks = sorted(df.loc[df["Completed"], "Week_Sort"].unique())
    if not completed_weeks:
        current_stats = compute_team_stats(df, teams, rank_basis=rank_basis)
        current_stats = add_strength_of_schedule(df, current_stats, rank_basis=rank_basis)
        rated = compute_dynasty_rating(current_stats, weights)
        rated["Rank_Change"] = 0
        return rated

    current_week = completed_weeks[-1]
    prior_weeks = [w for w in completed_weeks if w < current_week]
    prior_week = prior_weeks[-1] if prior_weeks else None

    current_stats = compute_team_stats(df, teams, as_of_week_sort=current_week, rank_basis=rank_basis)
    current_stats = add_strength_of_schedule(df, current_stats, as_of_week_sort=current_week, rank_basis=rank_basis)
    current_rated = compute_dynasty_rating(current_stats, weights)

    if prior_week is not None:
        prior_stats = compute_team_stats(df, teams, as_of_week_sort=prior_week, rank_basis=rank_basis)
        prior_stats = add_strength_of_schedule(df, prior_stats, as_of_week_sort=prior_week, rank_basis=rank_basis)
        prior_rated = compute_dynasty_rating(prior_stats, weights)
        prior_rank_map = prior_rated["Rank"].to_dict()
    else:
        prior_rank_map = {}

    current_rated["Rank_Change"] = current_rated.apply(
        lambda r: (prior_rank_map.get(r.name, r["Rank"]) - r["Rank"]) if r.name in prior_rank_map else 0,
        axis=1,
    )
    return current_rated


def rating_explanation(team: str, rated_stats: pd.DataFrame) -> list:
    """Returns a short list of bullet-point reasons behind a team's rating."""
    if team not in rated_stats.index:
        return []
    row = rated_stats.loc[team]
    bullets = []
    if row["User_W"] > 0:
        bullets.append(f"{int(row['User_W'])}-{int(row['User_L'])} vs user-controlled teams -- the biggest factor in the rating")
    if row["Road_Ranked_W"] > 0:
        big = int(row["Road_Ranked_W_Big"])
        extra = f" ({big} by 17+)" if big else ""
        bullets.append(f"{int(row['Road_Ranked_W'])} road win(s) over ranked opponents{extra}")
    if row["Home_Ranked_W"] > 0:
        big = int(row["Home_Ranked_W_Big"])
        extra = f" ({big} by 17+)" if big else ""
        bullets.append(f"{int(row['Home_Ranked_W'])} home win(s) over ranked opponents{extra}")
    if row["Road_Unranked_W"] > 0:
        big = int(row["Road_Unranked_W_Big"])
        extra = f" ({big} by 28+)" if big else ""
        bullets.append(f"{int(row['Road_Unranked_W'])} road win(s) over unranked opponents{extra}")
    if row["Home_Unranked_W"] > 0:
        big = int(row["Home_Unranked_W_Big"])
        extra = f" ({big} by 28+)" if big else ""
        bullets.append(f"{int(row['Home_Unranked_W'])} home win(s) over unranked opponents{extra}")
    if pd.notna(row.get("Win_Pct")):
        bullets.append(f"{int(row['W'])}-{int(row['L'])} overall")
    if row["Streak"] not in ("-", None):
        bullets.append(f"Currently riding a {row['Streak']} streak")
    return bullets


# ---------------------------------------------------------------------------
# Head-to-head
# ---------------------------------------------------------------------------

def build_h2h_matrix(df: pd.DataFrame, teams: list) -> pd.DataFrame:
    """
    Returns a team x team matrix of 'W'/'L'/'-' for completed User-vs-User
    games (from the row-owner's perspective, i.e. matrix[a][b] = a's result
    vs b).
    """
    matrix = pd.DataFrame("-", index=teams, columns=teams)
    uu = df[(df["Completed"]) & (df["Opponent_Is_User"])]
    for _, row in uu.iterrows():
        if row["Team"] in matrix.index and row["Opponent"] in matrix.columns:
            matrix.loc[row["Team"], row["Opponent"]] = row["Outcome"]
    for t in teams:
        if t in matrix.columns:
            matrix.loc[t, t] = "—"
    return matrix


def user_vs_user_records(df: pd.DataFrame, teams: list) -> pd.DataFrame:
    uu = df[(df["Completed"]) & (df["Opponent_Is_User"])]
    team_user_map = df.drop_duplicates("Team").set_index("Team")["User"].to_dict()

    def _label(opp_team: str) -> str:
        user = team_user_map.get(opp_team)
        return f"{opp_team} ({user})" if user else opp_team

    rows = []
    for team in teams:
        tg = uu[uu["Team"] == team]
        wins = sorted(tg.loc[tg["Outcome"] == "W", "Opponent"].tolist())
        losses = sorted(tg.loc[tg["Outcome"] == "L", "Opponent"].tolist())
        rows.append({
            "Team": team,
            "User": team_user_map.get(team, ""),
            "UU_W": int((tg["Outcome"] == "W").sum()),
            "UU_L": int((tg["Outcome"] == "L").sum()),
            "Wins_Over": ", ".join(_label(o) for o in wins),
            "Losses_To": ", ".join(_label(o) for o in losses),
        })
    return pd.DataFrame(rows).set_index("Team")


# ---------------------------------------------------------------------------
# League-wide stats
# ---------------------------------------------------------------------------

def league_summary(df: pd.DataFrame) -> dict:
    total_games = len(get_unique_games(df, completed_only=False))
    completed = get_unique_games(df, completed_only=True)
    user_games = df[(df["Completed"]) & (df["Opponent_Is_User"])].drop_duplicates("Game_Id")
    cpu_games = df[(df["Completed"]) & (~df["Opponent_Is_User"])]
    return {
        "total_games": total_games,
        "games_completed": len(completed),
        "user_vs_user_games": len(user_games),
        "cpu_games": len(cpu_games),
    }


def home_field_advantage(df: pd.DataFrame) -> dict:
    completed = df[df["Completed"]]
    home_w = int((completed["Home_Game"] & completed["Win"]).sum())
    home_l = int((completed["Home_Game"] & completed["Loss"]).sum())
    away_w = int((completed["Away_Game"] & completed["Win"]).sum())
    away_l = int((completed["Away_Game"] & completed["Loss"]).sum())
    return {"home_w": home_w, "home_l": home_l, "away_w": away_w, "away_l": away_l}


def biggest_blowout(df: pd.DataFrame):
    games = get_unique_games(df, completed_only=True)
    if games.empty:
        return None
    idx = games["Margin"].abs().idxmax()
    row = games.loc[idx]
    winner, loser, wscore, lscore = _winner_loser(row)
    return {"winner": winner, "loser": loser, "score": f"{wscore:.0f}-{lscore:.0f}", "margin": abs(row["Margin"])}


def closest_game(df: pd.DataFrame):
    games = get_unique_games(df, completed_only=True)
    if games.empty:
        return None
    idx = games["Margin"].abs().idxmin()
    row = games.loc[idx]
    winner, loser, wscore, lscore = _winner_loser(row)
    return {"winner": winner, "loser": loser, "score": f"{wscore:.0f}-{lscore:.0f}", "margin": abs(row["Margin"])}


def _winner_loser(row):
    if row["Outcome"] == "W":
        return row["Team"], row["Opponent"], row["Team_Score"], row["Opponent_Score"]
    else:
        return row["Opponent"], row["Team"], row["Opponent_Score"], row["Team_Score"]


def highest_scoring_game(df: pd.DataFrame):
    games = get_unique_games(df, completed_only=True)
    if games.empty:
        return None
    games = games.copy()
    games["Total_Pts"] = games["Team_Score"] + games["Opponent_Score"]
    idx = games["Total_Pts"].idxmax()
    row = games.loc[idx]
    winner, loser, wscore, lscore = _winner_loser(row)
    return {"team_a": row["Team"], "score_a": row["Team_Score"], "team_b": row["Opponent"], "score_b": row["Opponent_Score"]}


def best_defensive_performance(df: pd.DataFrame):
    completed = df[df["Completed"]]
    if completed.empty:
        return None
    idx = completed["Opponent_Score"].idxmin()
    row = completed.loc[idx]
    return {"team": row["Team"], "points_allowed": row["Opponent_Score"], "opponent": row["Opponent"]}


# ---------------------------------------------------------------------------
# Weekly recap
# ---------------------------------------------------------------------------

def weekly_recap(df: pd.DataFrame, team_stats_current: pd.DataFrame, week_sort: int,
                  rank_basis: str = "at_game") -> dict:
    week_games = get_unique_games(df, completed_only=True)
    week_games = week_games[week_games["Week_Sort"] == week_sort]
    if week_games.empty:
        return {}

    week_games = week_games.copy()
    week_games["Total_Pts"] = week_games["Team_Score"] + week_games["Opponent_Score"]

    ranked_win_col = "Ranked_Win_AtGame" if rank_basis == "at_game" else "Ranked_Win"
    rank_num_col = "Opponent_Rank_At_Game_Num" if rank_basis == "at_game" else "Opponent_Rank_Num"

    # Game of the week: closest user-vs-user game that week, else closest overall
    uu_games = week_games[week_games["Opponent_Is_User"]]
    gow_pool = uu_games if not uu_games.empty else week_games
    gow_row = gow_pool.loc[gow_pool["Margin"].abs().idxmin()]
    gow_winner, gow_loser, gow_wscore, gow_lscore = _winner_loser(gow_row)

    # Upset alert: a team with a worse (numerically higher, i.e. lower-rated)
    # Dynasty Rank beat a team with a meaningfully better rank, OR beat a
    # ranked CPU opponent (rank as of the game itself, not their rank today).
    upset = None
    for _, row in week_games.iterrows():
        if row["Outcome"] != "W":
            continue
        if row["Opponent_Is_User"] and row["Opponent"] in team_stats_current.index and row["Team"] in team_stats_current.index:
            team_rank = team_stats_current.loc[row["Team"], "Rank"]
            opp_rank = team_stats_current.loc[row["Opponent"], "Rank"]
            if team_rank - opp_rank >= 3:
                upset = f"{row['Team']} (#{int(team_rank)}) defeated {row['Opponent']} (#{int(opp_rank)})"
                break
        elif row[ranked_win_col] and pd.notna(row[rank_num_col]) and row[rank_num_col] <= 15:
            upset = f"{row['Team']} defeated #{int(row[rank_num_col])} {row['Opponent']}"
            break

    highest_scoring = week_games.loc[week_games["Total_Pts"].idxmax()]
    best_defense = week_games.loc[week_games["Opponent_Score"].idxmin() if week_games["Outcome"].eq("W").any() else week_games.index[0]]
    # best defense should reflect the winning team's points allowed where possible
    def_candidates = week_games[week_games["Win"]] if "Win" in week_games.columns else week_games
    if not def_candidates.empty:
        best_defense = def_candidates.loc[def_candidates["Opponent_Score"].idxmin()]

    return {
        "week": week_sort,
        "game_of_week": {"winner": gow_winner, "loser": gow_loser, "score": f"{gow_wscore:.0f}-{gow_lscore:.0f}"},
        "upset": upset,
        "highest_scoring": {
            "team_a": highest_scoring["Team"], "score_a": highest_scoring["Team_Score"],
            "team_b": highest_scoring["Opponent"], "score_b": highest_scoring["Opponent_Score"],
        },
        "best_defense": {
            "team": best_defense["Team"], "points_allowed": best_defense["Opponent_Score"],
            "opponent": best_defense["Opponent"],
        },
    }


# ---------------------------------------------------------------------------
# Fun stats
# ---------------------------------------------------------------------------

def fun_stats(df: pd.DataFrame, team_stats: pd.DataFrame) -> dict:
    completed = df[df["Completed"]]
    result = {}

    if not team_stats.empty and team_stats["Ranked_W"].notna().any():
        gk = team_stats["Ranked_W"].idxmax()
        result["giant_killer"] = {"team": gk, "value": int(team_stats.loc[gk, "Ranked_W"])}

    away_records = team_stats.assign(away_gp=team_stats["Away_W"] + team_stats["Away_L"])
    away_records = away_records[away_records["away_gp"] > 0]
    if not away_records.empty:
        away_records = away_records.assign(away_pct=away_records["Away_W"] / away_records["away_gp"])
        rw = away_records["away_pct"].idxmax()
        result["road_warrior"] = {
            "team": rw,
            "record": f"{int(away_records.loc[rw, 'Away_W'])}-{int(away_records.loc[rw, 'Away_L'])}",
        }

    home_records = team_stats.assign(home_gp=team_stats["Home_W"] + team_stats["Home_L"])
    home_records = home_records[home_records["home_gp"] > 0]
    if not home_records.empty:
        home_records = home_records.assign(home_pct=home_records["Home_W"] / home_records["home_gp"])
        fort = home_records["home_pct"].idxmax()
        result["fortress"] = {
            "team": fort,
            "record": f"{int(home_records.loc[fort, 'Home_W'])}-{int(home_records.loc[fort, 'Home_L'])}",
        }

    close_teams = team_stats[(team_stats["GP"] > 0) & (team_stats["MOV"].abs() <= 7)]
    if not close_teams.empty:
        ha = close_teams["MOV"].abs().idxmin()
        result["heart_attack_team"] = {"team": ha, "avg_margin": float(close_teams.loc[ha, "MOV"])}

    blowout_teams = team_stats[team_stats["GP"] > 0]
    if not blowout_teams.empty:
        bk = blowout_teams["MOV"].idxmax()
        if pd.notna(blowout_teams.loc[bk, "MOV"]):
            result["blowout_king"] = {"team": bk, "avg_margin": float(blowout_teams.loc[bk, "MOV"])}

    cardiac = team_stats[team_stats["GP"] > 0]
    if not cardiac.empty and cardiac["One_Score_Games"].notna().any():
        ck = cardiac["One_Score_Games"].idxmax()
        result["cardiac_kids"] = {"team": ck, "value": int(cardiac.loc[ck, "One_Score_Games"])}

    upset_victims = completed[completed["Ranked_Loss"]] if "Ranked_Loss" in completed.columns else pd.DataFrame()
    if not upset_victims.empty:
        row = upset_victims.loc[upset_victims["Opponent_Rank_Num"].idxmin()] if False else None
    # Highest ranked (numerically lowest rank number) team to lose = look at
    # CPU-ranked losses' Opponent_Rank on the *winner's* row is not directly
    # the loser's own rank; use Ranked_Game losses only among the tracked
    # teams' user-vs-user matches isn't rank-based. Skipping if ambiguous.

    sched = team_stats[team_stats["GP"] > 0]
    if not sched.empty and "SOS" in sched.columns and sched["SOS"].notna().any():
        toughest = sched["SOS"].idxmax()
        cupcake = sched["SOS"].idxmin()
        result["toughest_schedule"] = {"team": toughest, "sos": float(sched.loc[toughest, "SOS"])}
        result["cupcake_schedule"] = {"team": cupcake, "sos": float(sched.loc[cupcake, "SOS"])}

    scoring_machine = completed[completed["Team_Score"] > 45].groupby("Team").size()
    if not scoring_machine.empty:
        result["scoring_machine"] = {"team": scoring_machine.idxmax(), "value": int(scoring_machine.max())}

    brick_wall = completed[completed["Opponent_Score"] < 10].groupby("Team").size()
    if not brick_wall.empty:
        result["brick_wall"] = {"team": brick_wall.idxmax(), "value": int(brick_wall.max())}

    return result
