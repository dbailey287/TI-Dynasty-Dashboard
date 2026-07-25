"""
CFB Dynasty Command Center
===========================
A Streamlit dashboard for tracking an online College Football dynasty
league: standings, power rankings, schedules, head-to-head records,
league stats, weekly recaps, and fun stats.

Run with:
    streamlit run dynasty_dashboard.py

By default it loads "dynasty_data.csv" from the same folder. You can also
upload a fresh export any time from the sidebar.
"""
import os

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st

import dynasty_logic as dl

# ---------------------------------------------------------------------------
# Page config & light styling
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="CFB Dynasty Command Center",
    page_icon="🏈",
    layout="wide",
)

st.markdown("""
<style>
    .metric-card {
        background: #11151c;
        border: 1px solid #2a2f3a;
        border-radius: 10px;
        padding: 14px 16px;
        margin-bottom: 10px;
    }
    .rank-up { color: #2ecc71; font-weight: 600; }
    .rank-down { color: #e74c3c; font-weight: 600; }
    .rank-flat { color: #888; font-weight: 600; }
    div[data-testid="stMetricValue"] { font-size: 1.6rem; }
    .stat-label { color: #9aa0a6; font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.03em; }
    .user-game-card {
        border: 1px solid #d4af37;
        border-radius: 8px;
        padding: 10px 14px;
        margin-bottom: 8px;
        background: rgba(212, 175, 55, 0.12);
    }
    .user-badge {
        display: inline-block;
        background: #d4af37;
        color: #111;
        font-size: 0.68rem;
        font-weight: 700;
        padding: 1px 8px;
        border-radius: 10px;
        margin-right: 8px;
        letter-spacing: 0.03em;
        vertical-align: middle;
    }
    .cpu-game-row {
        padding: 6px 4px;
        margin-bottom: 2px;
        border-bottom: 1px solid #22262f;
    }
</style>
""", unsafe_allow_html=True)

DEFAULT_CSV_PATH = os.path.join(os.path.dirname(__file__), "dynasty_data.csv")


# ---------------------------------------------------------------------------
# Data loading (cached)
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def _load(file_bytes_or_path, is_upload: bool):
    if is_upload:
        import io
        return dl.load_and_prepare(io.BytesIO(file_bytes_or_path))
    return dl.load_and_prepare(file_bytes_or_path)


def get_data() -> pd.DataFrame:
    uploaded = st.session_state.get("uploaded_csv_bytes")
    if uploaded is not None:
        return _load(uploaded, is_upload=True)
    return _load(DEFAULT_CSV_PATH, is_upload=False)


def trend_arrow(change: int) -> str:
    if change > 0:
        return f'<span class="rank-up">▲{change}</span>'
    elif change < 0:
        return f'<span class="rank-down">▼{abs(change)}</span>'
    return '<span class="rank-flat">—</span>'


# ---------------------------------------------------------------------------
# Sidebar: navigation + data controls
# ---------------------------------------------------------------------------

st.sidebar.title("🏈 Dynasty Command Center")

PAGES = [
    "🏈 Home",
    "📊 Standings",
    "🏆 Power Rankings",
    "📅 Schedule",
    "👤 Teams",
    "🤝 Head-to-Head",
    "📈 League Stats",
    "🔥 Weekly Recap",
    "🎲 Fun Stats",
    "⚙️ Settings",
]
page = st.sidebar.radio("Navigate", PAGES, label_visibility="collapsed")

st.sidebar.divider()
with st.sidebar.expander("📤 Update data"):
    up = st.file_uploader("Upload a new export CSV", type=["csv"])
    if up is not None:
        st.session_state["uploaded_csv_bytes"] = up.getvalue()
        st.cache_data.clear()
        st.success("Loaded new file.")
    if st.session_state.get("uploaded_csv_bytes") is not None:
        if st.button("Revert to default file"):
            del st.session_state["uploaded_csv_bytes"]
            st.cache_data.clear()
            st.rerun()

if "rating_weights" not in st.session_state:
    st.session_state["rating_weights"] = dict(dl.DEFAULT_RATING_WEIGHTS)

# ---------------------------------------------------------------------------
# Load & compute shared data
# ---------------------------------------------------------------------------

try:
    df = get_data()
except Exception as e:
    st.error(f"Couldn't load the dynasty data file: {e}")
    st.stop()

TEAMS = sorted(df["Team"].unique())
weights = st.session_state["rating_weights"]

team_stats = dl.compute_team_stats(df, TEAMS)
team_stats = dl.add_strength_of_schedule(df, team_stats)
rated = dl.compute_rating_trend(df, TEAMS, weights)
h2h_matrix = dl.build_h2h_matrix(df, TEAMS)
summary = dl.league_summary(df)

completed_weeks = sorted(df.loc[df["Completed"], "Week_Sort"].unique())
last_completed_week_sort = completed_weeks[-1] if completed_weeks else None

# "Current week" = the week shown under "This Week" on Home. By default this
# is auto-detected as the earliest week that still has an unplayed game; it
# can be manually overridden from ⚙️ Settings.
week_sort_options = sorted(df["Week_Sort"].unique())
week_sort_label_map = {w: df.loc[df["Week_Sort"] == w, "Week"].iloc[0] for w in week_sort_options}

if "current_week_override" not in st.session_state:
    st.session_state["current_week_override"] = None

auto_current_week_sort = dl.default_current_week_sort(df)
effective_current_week_sort = (
    st.session_state["current_week_override"]
    if st.session_state["current_week_override"] is not None
    else auto_current_week_sort
)
effective_current_week_label = week_sort_label_map.get(effective_current_week_sort)


def team_display(team: str) -> str:
    user = team_stats.loc[team, "User"] if team in team_stats.index else ""
    return f"{team} ({user})" if user else team


def render_week_games(week_sort, empty_message: str):
    """Renders the game list for a given Week_Sort, highlighting User vs
    User matchups. Shared by the 'This Week' and 'Upcoming Week' sections."""
    if week_sort is None:
        st.caption("No data yet.")
        return
    wk_games = df[df["Week_Sort"] == week_sort]
    wk_games = wk_games[wk_games["Status"] != "BYE"].drop_duplicates(subset="Game_Id")
    if wk_games.empty:
        st.caption(empty_message)
        return

    # User vs User matchups float to the top
    wk_games = wk_games.sort_values(by="Opponent_Is_User", ascending=False)
    for _, g in wk_games.iterrows():
        loc = "vs" if g["Location"] == "Home" else "@"
        rank_tag = f"#{int(g['Opponent_Rank_Num'])} " if pd.notna(g["Opponent_Rank_Num"]) else ""
        result_tag = ""
        if g["Status"] == "Completed":
            result_tag = f" &nbsp;·&nbsp; <b>{g['Outcome']}</b> {g['Team_Score']:.0f}-{g['Opponent_Score']:.0f}"

        if g["Opponent_Is_User"]:
            st.markdown(
                f'<div class="user-game-card">'
                f'<span class="user-badge">USER MATCHUP</span>'
                f'<b>{g["Team"]}</b> <span class="stat-label">({g["User"]})</span> '
                f'{loc} '
                f'<b>{g["Opponent"]}</b> <span class="stat-label">({g["Opponent_User"]})</span>'
                f'{result_tag}</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f'<div class="cpu-game-row">'
                f'<b>{g["Team"]}</b> <span class="stat-label">({g["User"]})</span> '
                f'{loc} {rank_tag}{g["Opponent"]}'
                f'{result_tag}</div>',
                unsafe_allow_html=True,
            )


# ============================================================================
# PAGE: HOME
# ============================================================================
if page == "🏈 Home":
    st.title("🏈 CFB 27 Dynasty Command Center")
    season = df["Season"].dropna().iloc[0] if df["Season"].notna().any() else "—"
    st.caption(f"Season {season} · Week {effective_current_week_label or '—'}")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Games Completed", f"{summary['games_completed']} / {summary['total_games']}")
    c2.metric("User vs User", summary["user_vs_user_games"])
    c3.metric("CPU Games", summary["cpu_games"])
    c4.metric("Teams Tracked", len(TEAMS))

    st.divider()

    col1, col2 = st.columns([1, 1.4])

    with col1:
        st.subheader("⭐ Current #1")
        if not rated.empty:
            top = rated.iloc[0]
            top_team = rated.index[0]
            st.markdown(f"### {top_team}")
            st.caption(team_stats.loc[top_team, "User"])
            st.metric("Record", f"{int(top['W'])}-{int(top['L'])}")
            st.metric("Dynasty Rating", f"{top['Dynasty_Rating']:.1f}")
            for b in dl.rating_explanation(top_team, rated)[:3]:
                st.markdown(f"- {b}")

        st.subheader("💥 Largest Upset")
        blowout = dl.biggest_blowout(df)
        upset_found = None
        for _, row in dl.get_unique_games(df).iterrows():
            if row["Ranked_Win"] and pd.notna(row["Opponent_Rank_Num"]) and row["Opponent_Rank_Num"] <= 10:
                upset_found = row
                break
        if upset_found is not None:
            w, l, ws, ls = dl._winner_loser(upset_found)
            st.markdown(f"**{w}** {ws:.0f}-{ls:.0f} over **#{int(upset_found['Opponent_Rank_Num'])} {l}**")
        elif blowout:
            st.markdown(f"**{blowout['winner']}** {blowout['score']} over **{blowout['loser']}**")
        else:
            st.caption("No games completed yet.")

    with col2:
        st.subheader(f"📅 This Week — Week {effective_current_week_label or '—'}")
        render_week_games(effective_current_week_sort, "No games scheduled this week.")

        st.divider()

        next_week_sort = next(
            (w for w in week_sort_options if effective_current_week_sort is not None and w > effective_current_week_sort),
            None,
        )
        next_week_label = week_sort_label_map.get(next_week_sort)
        st.subheader(f"⏭️ Upcoming Week — Week {next_week_label or '—'}")
        render_week_games(next_week_sort, "No games scheduled next week.")

        st.divider()
        st.subheader("🏆 Top 5 Power Rankings")
        top5 = rated.head(5).reset_index()
        for i, r in top5.iterrows():
            arrow = trend_arrow(int(r["Rank_Change"]))
            st.markdown(
                f"**{int(r['Rank'])}. {r['Team']}** — {r['Dynasty_Rating']:.1f} {arrow}",
                unsafe_allow_html=True,
            )


# ============================================================================
# PAGE: STANDINGS
# ============================================================================
elif page == "📊 Standings":
    st.title("📊 Standings")

    display = team_stats.copy()
    display["Overall"] = display["W"].astype(int).astype(str) + "-" + display["L"].astype(int).astype(str)
    display["Home"] = display["Home_W"].astype(int).astype(str) + "-" + display["Home_L"].astype(int).astype(str)
    display["Away"] = display["Away_W"].astype(int).astype(str) + "-" + display["Away_L"].astype(int).astype(str)
    display["vs User"] = display["User_W"].astype(int).astype(str) + "-" + display["User_L"].astype(int).astype(str)
    display["vs CPU"] = display["CPU_W"].astype(int).astype(str) + "-" + display["CPU_L"].astype(int).astype(str)
    display["PF"] = display["PF"].round(1)
    display["PA"] = display["PA"].round(1)
    display["MOV"] = display["MOV"].round(1)
    display["SOS"] = display["SOS"].round(3)

    show_cols = ["User", "Overall", "Home", "Away", "vs User", "vs CPU", "PF", "PA", "MOV", "SOS"]
    display = display.reset_index()[["Team"] + show_cols].sort_values(
        ["Team"], key=lambda s: s.map(lambda t: -team_stats.loc[t, "Win_Pct"] if pd.notna(team_stats.loc[t, "Win_Pct"]) else 999)
    )

    st.caption("Click any column header to sort.")
    st.dataframe(display, use_container_width=True, hide_index=True, height=600)


# ============================================================================
# PAGE: POWER RANKINGS
# ============================================================================
elif page == "🏆 Power Rankings":
    st.title("🏆 Power Rankings — Dynasty Rating")
    st.caption(
        "A weighted, 0-100 composite score (win %, strength of schedule, average "
        "margin, ranked wins, road wins, user-vs-user wins, recent form). "
        "Adjust the weighting in ⚙️ Settings."
    )

    ranked_display = rated.reset_index()
    ranked_display["Record"] = (
        ranked_display["W"].fillna(0).astype(int).astype(str)
        + "-" + ranked_display["L"].fillna(0).astype(int).astype(str)
    )
    ranked_display["Trend"] = ranked_display["Rank_Change"].apply(
        lambda c: f"▲{c}" if c > 0 else (f"▼{abs(c)}" if c < 0 else "—")
    )
    table = ranked_display[["Rank", "Team", "User", "Record", "Dynasty_Rating", "Trend"]].rename(
        columns={"Dynasty_Rating": "Rating"}
    )

    def _trend_color(val):
        if isinstance(val, str) and val.startswith("▲"):
            return "color: #2ecc71; font-weight: 600"
        if isinstance(val, str) and val.startswith("▼"):
            return "color: #e74c3c; font-weight: 600"
        return "color: #888; font-weight: 600"

    try:
        styled_table = table.style.map(_trend_color, subset=["Trend"])
    except AttributeError:
        styled_table = table.style.applymap(_trend_color, subset=["Trend"])

    row_height = 35
    st.dataframe(
        styled_table, use_container_width=True, hide_index=True,
        height=min(row_height * (len(table) + 1) + 3, 640),
    )

    st.divider()
    st.subheader("Why this ranking?")
    detail_team = st.selectbox(
        "Pick a team to see the reasoning behind its rating",
        ranked_display["Team"], format_func=team_display,
    )
    bullets = dl.rating_explanation(detail_team, rated)
    if bullets:
        for b in bullets:
            st.markdown(f"- {b}")
    else:
        st.caption("Not enough completed games yet.")

    st.divider()
    st.subheader("Rating Distribution")
    fig = px.bar(
        ranked_display.sort_values("Dynasty_Rating"),
        x="Dynasty_Rating", y="Team", orientation="h",
        color="Dynasty_Rating", color_continuous_scale="Viridis",
    )
    fig.update_layout(height=500, showlegend=False, coloraxis_showscale=False)
    st.plotly_chart(fig, use_container_width=True)


# ============================================================================
# PAGE: SCHEDULE
# ============================================================================
elif page == "📅 Schedule":
    st.title("📅 Schedule")

    f1, f2, f3, f4 = st.columns(4)
    week_options = ["All"] + sorted(df["Week"].unique(), key=dl.week_sort_key)
    week_filter = f1.selectbox("Week", week_options)
    team_options = ["All"] + TEAMS
    team_filter = f2.selectbox("Team", team_options)
    status_filter = f3.selectbox("Status", ["All", "Completed", "Upcoming", "BYE"])
    matchup_filter = f4.selectbox("Matchup Type", ["All", "User vs User", "User vs CPU", "BYE"])

    sched = df.copy()
    if week_filter != "All":
        sched = sched[sched["Week"] == week_filter]
    if team_filter != "All":
        sched = sched[(sched["Team"] == team_filter) | (sched["Opponent"] == team_filter)]
    if status_filter != "All":
        sched = sched[sched["Status"] == status_filter]
    if matchup_filter != "All":
        sched = sched[sched["Matchup_Type"] == matchup_filter]

    # Dedup user-vs-user rows unless filtering to a specific team (then keep
    # that team's own row so the perspective matches what they selected)
    if team_filter == "All":
        sched = sched.drop_duplicates(subset="Game_Id")

    sched = sched.sort_values("Week_Sort")
    sched_display = sched[[
        "Week", "Date", "Team", "User", "Location", "Opponent", "Opponent_User",
        "Opponent_Rank", "Status", "Outcome", "Team_Score", "Opponent_Score",
    ]].rename(columns={"Opponent_Rank": "Opp Rank", "Opponent_User": "Opponent User"})
    st.dataframe(sched_display, use_container_width=True, hide_index=True, height=650)


# ============================================================================
# PAGE: TEAMS (report cards)
# ============================================================================
elif page == "👤 Teams":
    st.title("👤 Team Report Card")

    selected_team = st.selectbox("Select a team", TEAMS, format_func=team_display)
    if selected_team in team_stats.index:
        row = team_stats.loc[selected_team]
        rating_row = rated.loc[selected_team] if selected_team in rated.index else None

        st.subheader(f"{selected_team}  ·  {row['User']}")
        if rating_row is not None:
            st.caption(f"Dynasty Rank #{int(rating_row['Rank'])}  ·  Rating {rating_row['Dynasty_Rating']:.1f}")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Overall", f"{int(row['W'])}-{int(row['L'])}")
        c2.metric("Home", f"{int(row['Home_W'])}-{int(row['Home_L'])}")
        c3.metric("Away", f"{int(row['Away_W'])}-{int(row['Away_L'])}")
        c4.metric("Current Streak", row["Streak"])

        c5, c6, c7, c8 = st.columns(4)
        c5.metric("vs User", f"{int(row['User_W'])}-{int(row['User_L'])}")
        c6.metric("vs CPU", f"{int(row['CPU_W'])}-{int(row['CPU_L'])}")
        c7.metric("vs Ranked", f"{int(row['Ranked_W'])}-{int(row['Ranked_L'])}")
        c8.metric("vs Top 10", f"{int(row['Top10_W'])}-{int(row['Top10_L'])}")

        c9, c10, c11, c12 = st.columns(4)
        c9.metric("Avg PF", f"{row['PF']:.1f}" if pd.notna(row["PF"]) else "—")
        c10.metric("Avg PA", f"{row['PA']:.1f}" if pd.notna(row["PA"]) else "—")
        c11.metric("Avg Margin", f"{row['MOV']:+.1f}" if pd.notna(row["MOV"]) else "—")
        c12.metric("SOS", f"{row['SOS']:.3f}" if pd.notna(row["SOS"]) else "—")

        st.divider()
        st.subheader("Game Log")
        log = df[df["Team"] == selected_team].sort_values("Week_Sort")
        log_display = log[[
            "Week", "Date", "Location", "Opponent", "Opponent_Rank",
            "Status", "Outcome", "Team_Score", "Opponent_Score",
        ]].rename(columns={"Opponent_Rank": "Opp Rank"})
        st.dataframe(log_display, use_container_width=True, hide_index=True, height=450)

        if rating_row is not None:
            st.divider()
            st.subheader("Why this ranking?")
            for b in dl.rating_explanation(selected_team, rated):
                st.markdown(f"- {b}")


# ============================================================================
# PAGE: HEAD-TO-HEAD
# ============================================================================
elif page == "🤝 Head-to-Head":
    st.title("🤝 Head-to-Head")

    uu_records = dl.user_vs_user_records(df, TEAMS)
    st.subheader("User vs User Records")
    uu_display = uu_records.copy()
    uu_display["Record"] = uu_display["UU_W"].astype(str) + "-" + uu_display["UU_L"].astype(str)
    uu_display = uu_display[["User", "Record", "Wins_Over", "Losses_To"]].sort_values(
        "Record", key=lambda s: s.map(lambda r: -int(r.split("-")[0]))
    )
    st.dataframe(uu_display, use_container_width=True)

    st.divider()
    st.subheader("League Matrix")
    st.caption("Row team's result vs. column team (W / L / — no game yet).")

    team_user_map = team_stats["User"].to_dict()
    matrix_labels = {t: (f"{t} ({team_user_map[t]})" if team_user_map.get(t) else t) for t in TEAMS}
    matrix_display = h2h_matrix.rename(index=matrix_labels, columns=matrix_labels)

    def _color_cell(val):
        if val == "W":
            return "background-color: rgba(46, 204, 113, 0.25)"
        if val == "L":
            return "background-color: rgba(231, 76, 60, 0.25)"
        return ""

    try:
        styled = matrix_display.style.map(_color_cell)  # pandas >= 2.1
    except AttributeError:
        styled = matrix_display.style.applymap(_color_cell)  # older pandas
    st.dataframe(styled, use_container_width=True, height=600)


# ============================================================================
# PAGE: LEAGUE STATS
# ============================================================================
elif page == "📈 League Stats":
    st.title("📈 League Stats")

    best_off = team_stats["PF"].idxmax() if team_stats["PF"].notna().any() else None
    best_def = team_stats["PA"].idxmin() if team_stats["PA"].notna().any() else None
    blowout = dl.biggest_blowout(df)
    closest = dl.closest_game(df)
    hfa = dl.home_field_advantage(df)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### Best Offense")
        if best_off:
            st.markdown(f"**{best_off}** — {team_stats.loc[best_off, 'PF']:.1f} PPG")
        else:
            st.caption("No data yet")
    with c2:
        st.markdown("#### Best Defense")
        if best_def:
            st.markdown(f"**{best_def}** — {team_stats.loc[best_def, 'PA']:.1f} PA/G")
        else:
            st.caption("No data yet")

    c3, c4 = st.columns(2)
    with c3:
        st.markdown("#### Biggest Blowout")
        if blowout:
            st.markdown(f"**{blowout['score']}** — {blowout['winner']} vs {blowout['loser']}")
        else:
            st.caption("No data yet")
    with c4:
        st.markdown("#### Closest Game")
        if closest:
            st.markdown(f"**{closest['score']}** — {closest['winner']} over {closest['loser']}")
        else:
            st.caption("No data yet")

    st.divider()
    st.markdown("#### Home Field Advantage")
    c5, c6 = st.columns(2)
    c5.metric("Home Record", f"{hfa['home_w']}-{hfa['home_l']}")
    c6.metric("Away Record", f"{hfa['away_w']}-{hfa['away_l']}")

    hfa_fig = go.Figure(data=[
        go.Bar(name="Wins", x=["Home", "Away"], y=[hfa["home_w"], hfa["away_w"]], marker_color="#2ecc71"),
        go.Bar(name="Losses", x=["Home", "Away"], y=[hfa["home_l"], hfa["away_l"]], marker_color="#e74c3c"),
    ])
    hfa_fig.update_layout(barmode="stack", height=350)
    st.plotly_chart(hfa_fig, use_container_width=True)

    st.divider()
    st.markdown("#### Offense vs Defense")
    scatter_df = team_stats.reset_index().dropna(subset=["PF", "PA"])
    if not scatter_df.empty:
        fig = px.scatter(
            scatter_df, x="PA", y="PF", text="Team", size="GP",
            labels={"PA": "Points Allowed / Game", "PF": "Points Scored / Game"},
        )
        fig.update_traces(textposition="top center")
        fig.update_layout(height=500)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.caption("Not enough completed games yet for this chart.")


# ============================================================================
# PAGE: WEEKLY RECAP
# ============================================================================
elif page == "🔥 Weekly Recap":
    st.title("🔥 Weekly Recap")

    if not completed_weeks:
        st.info("No completed games yet — recaps will appear once games are played.")
    else:
        week_labels = {}
        for w in completed_weeks:
            lbl = df.loc[df["Week_Sort"] == w, "Week"].iloc[0]
            week_labels[w] = lbl
        sel_week_sort = st.selectbox(
            "Week", completed_weeks, index=len(completed_weeks) - 1,
            format_func=lambda w: f"Week {week_labels[w]}",
        )

        recap = dl.weekly_recap(df, rated, sel_week_sort)
        if not recap:
            st.caption("No completed games that week.")
        else:
            st.subheader(f"Week {week_labels[sel_week_sort]}")

            with st.container(border=True):
                st.markdown("##### 🎮 Game of the Week")
                g = recap["game_of_week"]
                st.markdown(f"**{g['winner']}** def. **{g['loser']}**, {g['score']}")

            if recap["upset"]:
                with st.container(border=True):
                    st.markdown("##### 😱 Upset Alert")
                    st.markdown(recap["upset"])

            with st.container(border=True):
                st.markdown("##### 🔢 Highest Scoring")
                hs = recap["highest_scoring"]
                st.markdown(f"**{hs['team_a']}** {hs['score_a']:.0f} — {hs['score_b']:.0f} **{hs['team_b']}**")

            with st.container(border=True):
                st.markdown("##### 🛡️ Defensive Performance")
                bd = recap["best_defense"]
                st.markdown(f"**{bd['team']}** held **{bd['opponent']}** to {bd['points_allowed']:.0f} points")


# ============================================================================
# PAGE: FUN STATS
# ============================================================================
elif page == "🎲 Fun Stats":
    st.title("🎲 Fun Stats")

    fs = dl.fun_stats(df, rated)

    cards = [
        ("giant_killer", "🗡️ Giant Killer", "Most ranked wins", lambda v: f"{v['team']} — {v['value']}"),
        ("road_warrior", "🛣️ Road Warrior", "Best road record", lambda v: f"{v['team']} — {v['record']}"),
        ("fortress", "🏰 Fortress", "Best home record", lambda v: f"{v['team']} — {v['record']}"),
        ("heart_attack_team", "💓 Heart Attack Team", "Avg margin under 7", lambda v: f"{v['team']} — {v['avg_margin']:+.1f}"),
        ("blowout_king", "💪 Blowout King", "Largest average victory", lambda v: f"{v['team']} — {v['avg_margin']:+.1f}"),
        ("cardiac_kids", "❤️ Cardiac Kids", "Most one-score games", lambda v: f"{v['team']} — {v['value']}"),
        ("toughest_schedule", "🧗 Toughest Schedule", "Highest SOS", lambda v: f"{v['team']} — {v['sos']:.3f}"),
        ("cupcake_schedule", "🧁 Cupcake Schedule", "Lowest SOS", lambda v: f"{v['team']} — {v['sos']:.3f}"),
        ("scoring_machine", "🎯 Scoring Machine", "Most games over 45 pts", lambda v: f"{v['team']} — {v['value']}"),
        ("brick_wall", "🧱 Brick Wall", "Most games allowing under 10", lambda v: f"{v['team']} — {v['value']}"),
    ]

    cols = st.columns(2)
    for i, (key, title, subtitle, fmt) in enumerate(cards):
        with cols[i % 2]:
            with st.container(border=True):
                st.markdown(f"**{title}**")
                st.caption(subtitle)
                if key in fs:
                    st.markdown(f"### {fmt(fs[key])}")
                else:
                    st.caption("Not enough data yet")


# ============================================================================
# PAGE: SETTINGS
# ============================================================================
elif page == "⚙️ Settings":
    st.title("⚙️ Settings")

    st.subheader("Dynasty Rating Weights")
    st.caption("These control the Power Rankings formula. They're re-normalized automatically so they always sum to 100%.")

    w = st.session_state["rating_weights"]
    labels = {
        "win_pct": "Win %",
        "sos": "Strength of Schedule",
        "avg_margin": "Average Margin",
        "ranked_wins": "Ranked Wins",
        "road_wins": "Road Wins",
        "user_wins": "User-vs-User Wins",
        "recent_form": "Recent Form (last 5)",
    }

    new_weights = {}
    for key, label in labels.items():
        new_weights[key] = st.slider(label, 0, 100, int(w[key] * 100), key=f"w_{key}")

    total = sum(new_weights.values()) or 1
    normalized = {k: v / total for k, v in new_weights.items()}

    st.caption(f"Raw total: {total}% → normalized automatically.")
    if st.button("Apply weights"):
        st.session_state["rating_weights"] = normalized
        st.success("Updated! Power Rankings will reflect the new weights.")
        st.rerun()

    if st.button("Reset to recommended defaults"):
        st.session_state["rating_weights"] = dict(dl.DEFAULT_RATING_WEIGHTS)
        st.success("Reset.")
        st.rerun()

    st.divider()
    st.subheader("Current Week")
    st.caption(
        "This controls what shows under \"This Week\" on the Home page. By default "
        "it's auto-detected as the earliest week that still has an unplayed game."
    )

    week_choice_labels = ["Auto-detect"] + [week_sort_label_map[w] for w in week_sort_options]
    current_override = st.session_state["current_week_override"]
    if current_override is None:
        current_index = 0
    else:
        lbl = week_sort_label_map.get(current_override)
        current_index = week_choice_labels.index(lbl) if lbl in week_choice_labels else 0

    chosen_label = st.selectbox("Current Week", week_choice_labels, index=current_index)
    if chosen_label == "Auto-detect":
        st.session_state["current_week_override"] = None
    else:
        matched = [w for w in week_sort_options if week_sort_label_map[w] == chosen_label]
        st.session_state["current_week_override"] = matched[0] if matched else None

    auto_label = week_sort_label_map.get(auto_current_week_sort, "—") if auto_current_week_sort is not None else "—"
    st.caption(f"Auto-detected value right now: Week {auto_label}")

    st.divider()
    st.subheader("Data Source")
    if st.session_state.get("uploaded_csv_bytes") is not None:
        st.info("Currently using an uploaded file (see sidebar to revert).")
    else:
        st.info(f"Currently using: `{DEFAULT_CSV_PATH}`")
    st.caption(f"Last export timestamp in file: {df['Last_Updated'].iloc[0] if 'Last_Updated' in df.columns and len(df) else '—'}")

    st.divider()
    st.subheader("About the Dynasty Rating")
    st.markdown("""
    - **Win %**: season winning percentage
    - **Strength of Schedule**: for opponents that are other user teams, their own win %;
      for CPU opponents, a proxy based on their AP rank tier (or a flat baseline if unranked)
    - **Average Margin**: average scoring margin, scaled relative to the rest of the league
    - **Ranked Wins**: wins over AP-ranked opponents
    - **Road Wins**: wins away from home
    - **User-vs-User Wins**: wins over other human-controlled teams
    - **Recent Form**: win % over the last 5 completed games

    Each component is scaled 0-100 relative to the rest of the league, then combined
    using the weights above.
    """)
