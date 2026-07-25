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


def compute_team_stats(df: pd.DataFrame, teams: list, as_of_week_sort: int | None = None) -> pd.DataFrame:
    """
    Builds one row per team with season-to-date stats.
    If as_of_week_sort is given, only games with Week_Sort <= that value
    are considered (used to compute "last week's" snapshot for trend arrows).
    """
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
        ranked = tg[tg["Ranked_Game"]]
        top10 = tg[tg["Top10_Game"]]

        pf = tg["Team_Score"].mean() if games_played else np.nan
        pa = tg["Opponent_Score"].mean() if games_played else np.nan
        mov = tg["Margin"].mean() if games_played else np.nan

        streak_label, form_pct = _streak_and_form(tg)

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
                              as_of_week_sort: int | None = None) -> pd.DataFrame:
    """
    Two-pass SOS: for opponents that are one of our tracked teams, use that
    opponent's own win% (already computed in team_stats). For CPU opponents,
    use a rank-tier quality proxy. Adds an 'SOS' column (0-1 scale, higher
    = tougher) to a copy of team_stats.
    """
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
                qualities.append(_cpu_opponent_quality(row["Opponent_Rank_Num"]))
        sos_values[team] = float(np.mean(qualities)) if qualities else np.nan

    stats["SOS"] = pd.Series(sos_values)
    return stats


# ---------------------------------------------------------------------------
# Dynasty Rating (power rankings)
# ---------------------------------------------------------------------------

def compute_rating_history(df: pd.DataFrame, teams: list, weights: dict = None) -> pd.DataFrame:
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
        stats = compute_team_stats(df, teams, as_of_week_sort=w)
        stats = add_strength_of_schedule(df, stats, as_of_week_sort=w)
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


def compute_multi_season_rating_history(df_all: pd.DataFrame, weights: dict = None) -> pd.DataFrame:
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
        hist = compute_rating_history(season_df, season_teams, weights)
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


DEFAULT_RATING_WEIGHTS = {
    "win_pct": 0.35,
    "sos": 0.20,
    "avg_margin": 0.20,
    "ranked_wins": 0.10,
    "road_wins": 0.05,
    "user_wins": 0.05,
    "recent_form": 0.05,
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
    sos_scaled = _minmax_scale(stats["SOS"].fillna(stats["SOS"].mean()))
    margin_scaled = _minmax_scale(stats["MOV"].fillna(stats["MOV"].mean() if stats["MOV"].notna().any() else 0))
    ranked_wins_scaled = _minmax_scale(stats["Ranked_W"].fillna(0))
    road_wins_scaled = _minmax_scale(stats["Away_W"].fillna(0))
    user_wins_scaled = _minmax_scale(stats["User_W"].fillna(0))
    form_scaled = _minmax_scale(stats["Form_Pct"].fillna(0))

    rating = (
        weights["win_pct"] * win_pct_scaled
        + weights["sos"] * sos_scaled
        + weights["avg_margin"] * margin_scaled
        + weights["ranked_wins"] * ranked_wins_scaled
        + weights["road_wins"] * road_wins_scaled
        + weights["user_wins"] * user_wins_scaled
        + weights["recent_form"] * form_scaled
    )

    stats["Dynasty_Rating"] = rating.round(1)
    stats["Rating_Component_WinPct"] = win_pct_scaled
    stats["Rating_Component_SOS"] = sos_scaled
    stats["Rating_Component_Margin"] = margin_scaled

    stats = stats.sort_values("Dynasty_Rating", ascending=False)
    stats["Rank"] = range(1, len(stats) + 1)
    return stats


def compute_rating_trend(df: pd.DataFrame, teams: list, weights: dict = None) -> pd.DataFrame:
    """
    Computes current Dynasty Rating and the rating/rank as of one week
    earlier, returning current ranking with a Rank_Change column
    (positive = moved up).
    """
    completed_weeks = sorted(df.loc[df["Completed"], "Week_Sort"].unique())
    if not completed_weeks:
        current_stats = compute_team_stats(df, teams)
        current_stats = add_strength_of_schedule(df, current_stats)
        rated = compute_dynasty_rating(current_stats, weights)
        rated["Rank_Change"] = 0
        return rated

    current_week = completed_weeks[-1]
    prior_weeks = [w for w in completed_weeks if w < current_week]
    prior_week = prior_weeks[-1] if prior_weeks else None

    current_stats = compute_team_stats(df, teams, as_of_week_sort=current_week)
    current_stats = add_strength_of_schedule(df, current_stats, as_of_week_sort=current_week)
    current_rated = compute_dynasty_rating(current_stats, weights)

    if prior_week is not None:
        prior_stats = compute_team_stats(df, teams, as_of_week_sort=prior_week)
        prior_stats = add_strength_of_schedule(df, prior_stats, as_of_week_sort=prior_week)
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
    if row["Ranked_W"] > 0:
        bullets.append(f"{int(row['Ranked_W'])} win(s) over ranked opponents")
    if pd.notna(row["SOS"]):
        bullets.append(f"Strength of schedule score: {row['SOS']:.2f} (0-1 scale, higher = tougher)")
    if pd.notna(row["MOV"]):
        bullets.append(f"Average scoring margin of {row['MOV']:+.1f} pts/game")
    if row["Away_W"] > 0 and row["Away_L"] == 0 and row["Away_W"] >= 2:
        bullets.append(f"Undefeated on the road ({int(row['Away_W'])}-0)")
    if row["User_W"] > 0:
        bullets.append(f"{int(row['User_W'])}-{int(row['User_L'])} vs user-controlled teams")
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

def weekly_recap(df: pd.DataFrame, team_stats_current: pd.DataFrame, week_sort: int) -> dict:
    week_games = get_unique_games(df, completed_only=True)
    week_games = week_games[week_games["Week_Sort"] == week_sort]
    if week_games.empty:
        return {}

    week_games = week_games.copy()
    week_games["Total_Pts"] = week_games["Team_Score"] + week_games["Opponent_Score"]

    # Game of the week: closest user-vs-user game that week, else closest overall
    uu_games = week_games[week_games["Opponent_Is_User"]]
    gow_pool = uu_games if not uu_games.empty else week_games
    gow_row = gow_pool.loc[gow_pool["Margin"].abs().idxmin()]
    gow_winner, gow_loser, gow_wscore, gow_lscore = _winner_loser(gow_row)

    # Upset alert: a team with a worse (numerically higher, i.e. lower-rated)
    # Dynasty Rank beat a team with a meaningfully better rank, OR beat a
    # ranked CPU opponent.
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
        elif row["Ranked_Win"] and row["Opponent_Rank_Num"] <= 15:
            upset = f"{row['Team']} defeated #{int(row['Opponent_Rank_Num'])} {row['Opponent']}"
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
