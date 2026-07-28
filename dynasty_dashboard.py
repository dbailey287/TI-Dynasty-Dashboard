"""
CFB Dynasty Command Center
===========================
A Streamlit dashboard for tracking a multi-season online College Football
dynasty league: standings, power rankings, schedules, head-to-head records,
league stats, weekly recaps, fun stats, and career/historical stats.

Run with:
    streamlit run dynasty_dashboard.py

Data files: drop one CSV export per season in this same folder, named like
"dynasty_data_2026.csv", "dynasty_data_2027.csv", etc. Any file matching
"dynasty_data*.csv" here is auto-loaded and combined -- no manual merging
needed. You can also upload file(s) for a one-off session from the sidebar.
"""
import glob
import io
import os

import numpy as np
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
    .footer-note {
        text-align: center;
        color: #7a8290;
        font-size: 0.82rem;
        line-height: 1.5;
        padding: 6px 12px 2px 12px;
    }
</style>
""", unsafe_allow_html=True)

SCRIPT_DIR = os.path.dirname(__file__)


# ---------------------------------------------------------------------------
# Multi-season data loading (cached)
# ---------------------------------------------------------------------------

def discover_local_season_files() -> list:
    """Every file in this folder matching dynasty_data*.csv -- one per season."""
    return sorted(glob.glob(os.path.join(SCRIPT_DIR, "dynasty_data*.csv")))


@st.cache_data(show_spinner="Loading season data...")
def _load_combined(file_specs: tuple, is_upload: bool) -> pd.DataFrame:
    """
    file_specs: tuple of (name, bytes) if is_upload,
                tuple of (path, mtime) if loading from local disk.
    Local specs include each file's modification time specifically so
    that editing a CSV's contents (e.g. after running a data-correction
    script) busts the cache automatically -- without mtime, Streamlit's
    cache key is just the file path, so an edited-in-place file with the
    same name would silently keep serving the stale cached version until
    someone manually cleared the cache.
    """
    frames = []
    for spec in file_specs:
        if is_upload:
            name, content = spec
            raw = dl.load_raw_dataframe(io.BytesIO(content))
        else:
            path, _mtime = spec
            raw = dl.load_raw_dataframe(path)
        frames.append(dl.clean_dataframe(raw))
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    return dl.add_derived_columns(combined)


def get_all_seasons_data():
    """Returns (combined_df, loaded_file_labels, is_upload)."""
    uploaded = st.session_state.get("uploaded_season_files")
    if uploaded:
        specs = tuple(sorted(uploaded.items()))
        return _load_combined(specs, is_upload=True), list(uploaded.keys()), True

    local_files = discover_local_season_files()
    specs = tuple((f, os.path.getmtime(f)) for f in local_files)
    labels = [os.path.basename(f) for f in local_files]
    return _load_combined(specs, is_upload=False), labels, False


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
    "📜 Career",
    "⚙️ Settings",
]
page = st.sidebar.radio("Navigate", PAGES, label_visibility="collapsed", key="nav_radio")

st.sidebar.divider()

try:
    df_all, loaded_file_labels, using_uploads = get_all_seasons_data()
except Exception as e:
    st.error(f"Couldn't load season data: {e}")
    st.stop()

if df_all.empty:
    st.error(
        "No season data files found. Place one or more files named like "
        "`dynasty_data_2026.csv` in this folder, or upload one below."
    )
    with st.sidebar.expander("📤 Update data", expanded=True):
        ups = st.file_uploader("Upload season file(s)", type=["csv"], accept_multiple_files=True)
        if ups:
            st.session_state["uploaded_season_files"] = {u.name: u.getvalue() for u in ups}
            st.cache_data.clear()
            st.rerun()
    st.stop()

# Overlay each team's current Discord display_name from Server_Members_Teams.csv,
# if that file is present -- upgrades the "User" column to something a human
# actually recognizes, and self-corrects historical rows too (this replaces
# whatever text the scraper happened to read off a screenshot, which was
# never guaranteed to match anyone's real name in the first place).
_roster_display_map = dl.load_roster_display_names(SCRIPT_DIR)
if _roster_display_map:
    df_all = dl.apply_display_names(df_all, _roster_display_map)

seasons_available = sorted(df_all["Season"].dropna().unique().tolist(), reverse=True)
if "selected_season" not in st.session_state or st.session_state["selected_season"] not in seasons_available:
    st.session_state["selected_season"] = seasons_available[0]

selected_season = st.sidebar.selectbox(
    "Season", seasons_available,
    index=seasons_available.index(st.session_state["selected_season"]),
    key="season_selectbox",
)
st.session_state["selected_season"] = selected_season

with st.sidebar.expander("📤 Update data"):
    st.caption(
        "Auto-loads every `dynasty_data*.csv` file in this folder — one per "
        "season. Name new exports like `dynasty_data_2027.csv` and drop them "
        "in; no merging needed."
    )
    if loaded_file_labels:
        st.caption("Currently loaded: " + ", ".join(loaded_file_labels))

    ups = st.file_uploader("Or upload season file(s) for this session", type=["csv"], accept_multiple_files=True)
    if ups:
        st.session_state["uploaded_season_files"] = {u.name: u.getvalue() for u in ups}
        st.cache_data.clear()
        st.success(f"Loaded {len(ups)} uploaded file(s).")
        st.rerun()
    if using_uploads:
        if st.button("Revert to local season files"):
            del st.session_state["uploaded_season_files"]
            st.cache_data.clear()
            st.rerun()
    if st.button("🔄 Reload data files"):
        st.cache_data.clear()
        st.rerun()

if "rating_weights" not in st.session_state:
    st.session_state["rating_weights"] = dict(dl.DEFAULT_RATING_WEIGHTS)

# ---------------------------------------------------------------------------
# Load & compute shared data (scoped to the selected season)
# ---------------------------------------------------------------------------

df = df_all[df_all["Season"] == selected_season].copy()
weights = st.session_state["rating_weights"]

TEAMS = sorted(df["Team"].unique())

# Two ranking philosophies, both computed:
#   "at_game" -- a win over the #1 team stays a win over the #1 team,
#                even after that opponent later slips in the rankings.
#                This is the primary/default view used everywhere.
#   "live"    -- opponents' CURRENT rank, which can retroactively make a
#                past win look stronger or weaker. Secondary/opt-in view,
#                shown folded away on the Power Rankings page.
# Older seasons scraped before rank-freezing existed won't have usable
# at-game data -- fall back to live as the primary view for those rather
# than showing an empty/degenerate "at time of game" ranking.
HAS_AT_GAME_DATA = dl.has_at_game_rank_data(df)
PRIMARY_RANK_BASIS = "at_game" if HAS_AT_GAME_DATA else "live"

team_stats = dl.compute_team_stats(df, TEAMS, rank_basis=PRIMARY_RANK_BASIS)
team_stats = dl.add_strength_of_schedule(df, team_stats, rank_basis=PRIMARY_RANK_BASIS)
rated = dl.compute_rating_trend(df, TEAMS, weights, rank_basis=PRIMARY_RANK_BASIS)

team_stats_live = dl.compute_team_stats(df, TEAMS, rank_basis="live")
team_stats_live = dl.add_strength_of_schedule(df, team_stats_live, rank_basis="live")
rated_live = dl.compute_rating_trend(df, TEAMS, weights, rank_basis="live")

h2h_matrix = dl.build_h2h_matrix(df, TEAMS)
summary = dl.league_summary(df)

completed_weeks = sorted(df.loc[df["Completed"], "Week_Sort"].unique())
last_completed_week_sort = completed_weeks[-1] if completed_weeks else None

# "Current week" = the week shown under "This Week" on Home. By default this
# is auto-detected as the earliest week that still has an unplayed game; it
# can be manually overridden from ⚙️ Settings. Reset if it doesn't apply to
# the currently selected season.
week_sort_options = sorted(df["Week_Sort"].unique())
week_sort_label_map = {w: df.loc[df["Week_Sort"] == w, "Week"].iloc[0] for w in week_sort_options}

if "current_week_override" not in st.session_state:
    st.session_state["current_week_override"] = None
if (st.session_state["current_week_override"] is not None
        and st.session_state["current_week_override"] not in week_sort_options):
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


def render_team_hero(team: str, user: str, rank=None, rating=None):
    """Full-width gradient banner in the team's own colors, with a dark
    overlay so white text stays readable regardless of how light/dark the
    team's actual colors are."""
    primary = dl.team_primary_color(team)
    secondary = dl.team_secondary_color(team)
    logo = dl.logo_url(team)
    logo_html = (
        f'<img src="{logo}" style="height:70px;width:70px;object-fit:contain;'
        f'filter:drop-shadow(0 2px 6px rgba(0,0,0,0.5));">' if logo else ""
    )
    badge_html = ""
    if rank is not None and rating is not None:
        badge_html = (
            '<div style="margin-top:8px;">'
            f'<span style="background:rgba(0,0,0,0.35); padding:4px 12px; border-radius:20px; '
            f'font-size:0.85rem; font-weight:600; margin-right:8px;">Dynasty Rank #{int(rank)}</span>'
            f'<span style="background:rgba(0,0,0,0.35); padding:4px 12px; border-radius:20px; '
            f'font-size:0.85rem; font-weight:600;">Rating {rating:.1f}</span>'
            '</div>'
        )
    st.markdown(
        f'<div style="background: linear-gradient(135deg, {primary} 0%, {secondary} 100%); '
        'position: relative; border-radius: 14px; padding: 20px 24px; margin-bottom: 18px; '
        'display:flex; align-items:center; gap:18px; overflow:hidden;">'
        '<div style="position:absolute; inset:0; background:rgba(0,0,0,0.45);"></div>'
        f'<div style="position:relative; z-index:1;">{logo_html}</div>'
        '<div style="position:relative; z-index:1; color:white;">'
        f'<div style="font-size:1.7rem; font-weight:800; text-shadow: 0 2px 4px rgba(0,0,0,0.4);">{team}</div>'
        f'<div style="font-size:0.95rem; opacity:0.9;">{user}</div>'
        f'{badge_html}'
        '</div></div>',
        unsafe_allow_html=True,
    )


def colored_divider(color: str):
    st.markdown(
        f'<hr style="border:none; height:2px; background:{color}; opacity:0.5; margin:18px 0;">',
        unsafe_allow_html=True,
    )


def team_logo_tag(team: str, size: int = 22) -> str:
    """Small inline <img> tag for a team's logo, or '' if unrecognized."""
    url = dl.logo_url(team)
    if not url:
        return ""
    return (
        f'<img src="{url}" style="height:{size}px;width:{size}px;'
        f'vertical-align:middle;object-fit:contain;margin-right:6px;" '
        f'onerror="this.style.display=\'none\'">'
    )


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
        rank_tag = f"#{int(g['Opponent_Rank_Display_Num'])} " if pd.notna(g["Opponent_Rank_Display_Num"]) else ""
        result_tag = ""
        if g["Status"] == "Completed":
            result_tag = f" &nbsp;·&nbsp; <b>{g['Outcome']}</b> {g['Team_Score']:.0f}-{g['Opponent_Score']:.0f}"

        team_logo = team_logo_tag(g["Team"])
        opp_logo = team_logo_tag(g["Opponent"])

        if g["Opponent_Is_User"]:
            st.markdown(
                f'<div class="user-game-card">'
                f'<span class="user-badge">USER MATCHUP</span>'
                f'{team_logo}<b>{g["Team"]}</b> <span class="stat-label">({g["User"]})</span> '
                f'{loc} '
                f'{opp_logo}<b>{g["Opponent"]}</b> <span class="stat-label">({g["Opponent_User"]})</span>'
                f'{result_tag}</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f'<div class="cpu-game-row">'
                f'{team_logo}<b>{g["Team"]}</b> <span class="stat-label">({g["User"]})</span> '
                f'{loc} {rank_tag}{opp_logo}{g["Opponent"]}'
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

    rta_status_path = os.path.join(SCRIPT_DIR, "rta_status.json")
    rta_status = dl.load_rta_status(rta_status_path)
    if rta_status is not None:
        st.divider()
        ready_ids = set(rta_status.get("ready_user_ids", []))
        roster_entries = dl.load_roster_entries(SCRIPT_DIR)

        st.subheader(f"✅ Ready to Advance — {len(ready_ids & {e['user_id'] for e in roster_entries})}/{len(roster_entries)}")
        if roster_entries:
            items_html = []
            for entry in sorted(roster_entries, key=lambda e: e["team"]):
                team = entry["team"]
                is_ready = entry["user_id"] in ready_ids
                logo = team_logo_tag(team, 20) if team else ""
                icon = "✅" if is_ready else "⬜"
                items_html.append(
                    f'<div style="flex:1 1 220px; padding:3px 8px 3px 0;">{icon} {logo}<b>{team}</b> '
                    f'<span class="stat-label">({entry["display_name"]})</span></div>'
                )
            # flexbox with wrap, not st.columns() -- this always reads top-to-bottom,
            # left-to-right in the SAME sorted order regardless of screen width. Native
            # Streamlit columns look fine on desktop but silently reorder into
            # column-then-column blocks once they stack on a narrow/mobile screen,
            # which is what was actually happening here.
            st.markdown(
                f'<div style="display:flex; flex-wrap:wrap;">{"".join(items_html)}</div>',
                unsafe_allow_html=True,
            )
        else:
            st.caption("Roster not found (Server_Members_Teams.csv) -- can't show who's tracked.")
        if rta_status.get("last_updated"):
            st.caption(f"Last checked: {rta_status['last_updated']}")
    else:
        rta_error = dl.rta_status_diagnostic(rta_status_path)
        if rta_error:
            st.divider()
            st.warning(f"⚠️ {rta_error}")

    st.divider()

    col1, col2 = st.columns([1, 1.4])

    with col1:
        st.subheader("⭐ Current #1")
        if not rated.empty:
            top = rated.iloc[0]
            top_team = rated.index[0]
            primary = dl.team_primary_color(top_team)
            logo = dl.logo_url(top_team)

            lc1, lc2 = st.columns([1, 2])
            with lc1:
                if logo:
                    st.image(logo, width=90)
            with lc2:
                st.markdown(
                    f'<div style="border-left: 5px solid {primary}; padding-left: 10px;">'
                    f'<h3 style="margin:0;">{top_team}</h3>'
                    f'<span class="stat-label">{team_stats.loc[top_team, "User"]}</span></div>',
                    unsafe_allow_html=True,
                )
            st.metric("Record", f"{int(top['W'])}-{int(top['L'])}")
            st.metric("Dynasty Rating", f"{top['Dynasty_Rating']:.1f}")
            for b in dl.rating_explanation(top_team, rated)[:3]:
                st.markdown(f"- {b}")

        st.subheader("💥 Largest Upset")
        blowout = dl.biggest_blowout(df)
        upset_rank_col = "Ranked_Win_AtGame" if PRIMARY_RANK_BASIS == "at_game" else "Ranked_Win"
        upset_num_col = "Opponent_Rank_At_Game_Num" if PRIMARY_RANK_BASIS == "at_game" else "Opponent_Rank_Num"
        upset_found = None
        for _, row in dl.get_unique_games(df).iterrows():
            if row[upset_rank_col] and pd.notna(row[upset_num_col]) and row[upset_num_col] <= 10:
                upset_found = row
                break
        if upset_found is not None:
            w, l, ws, ls = dl._winner_loser(upset_found)
            st.markdown(
                f"{team_logo_tag(w, 26)}**{w}** {ws:.0f}-{ls:.0f} over "
                f"{team_logo_tag(l, 26)}**#{int(upset_found[upset_num_col])} {l}**",
                unsafe_allow_html=True,
            )
        elif blowout:
            st.markdown(
                f"{team_logo_tag(blowout['winner'], 26)}**{blowout['winner']}** {blowout['score']} over "
                f"{team_logo_tag(blowout['loser'], 26)}**{blowout['loser']}**",
                unsafe_allow_html=True,
            )
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
            logo = team_logo_tag(r["Team"], 22)
            st.markdown(
                f"**{int(r['Rank'])}.** {logo}**{r['Team']}** — {r['Dynasty_Rating']:.1f} {arrow}",
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
    display.insert(0, "Logo", display["Team"].apply(dl.logo_url))

    st.caption("Click any column header to sort.")
    st.dataframe(
        display, use_container_width=True, hide_index=True, height=600,
        column_config={"Logo": st.column_config.ImageColumn("", width="small")},
    )


# ============================================================================
# PAGE: POWER RANKINGS
# ============================================================================
elif page == "🏆 Power Rankings":
    st.title("🏆 Power Rankings — Dynasty Rating")
    st.caption(
        "A weighted, 0-100 composite score. Priority order: User-vs-User wins, then "
        "wins over ranked opponents (road weighted above home, plus a bonus for 17+ "
        "point margins), then wins over unranked opponents (road above home, plus a "
        "bonus for 28+ point margins), with overall win % as a baseline. Wins over "
        "higher-ranked opponents count for more than wins over lower-ranked ones. "
        "Adjust the weighting in ⚙️ Settings."
    )

    ranked_display = rated.reset_index()
    ranked_display["Record"] = (
        ranked_display["W"].fillna(0).astype(int).astype(str)
        + "-" + ranked_display["L"].fillna(0).astype(int).astype(str)
    )
    ranked_display["User Rec"] = (
        ranked_display["User_W"].fillna(0).astype(int).astype(str)
        + "-" + ranked_display["User_L"].fillna(0).astype(int).astype(str)
    )
    ranked_display["Trend"] = ranked_display["Rank_Change"].apply(
        lambda c: f"▲{c}" if c > 0 else (f"▼{abs(c)}" if c < 0 else "—")
    )
    ranked_display["Logo"] = ranked_display["Team"].apply(dl.logo_url)

    input_cols = {
        "Road_Ranked_W": "Rd Rk",
        "Home_Ranked_W": "Hm Rk",
        "Road_Ranked_W_Big": "Rd Rk 17+",
        "Home_Ranked_W_Big": "Hm Rk 17+",
        "Road_Unranked_W": "Rd Unrk",
        "Home_Unranked_W": "Hm Unrk",
        "Road_Unranked_W_Big": "Rd Unrk 28+",
        "Home_Unranked_W_Big": "Hm Unrk 28+",
    }
    ranked_display = ranked_display.rename(columns=input_cols)

    table = ranked_display[
        ["Rank", "Logo", "Team", "User", "Record", "User Rec"]
        + list(input_cols.values())
        + ["Dynasty_Rating", "Trend"]
    ].rename(columns={"Dynasty_Rating": "Rating"})

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
        column_config={
            "Logo": st.column_config.ImageColumn("", width="small"),
        },
    )
    st.caption(
        "Rd/Hm = Road/Home · Rk = vs Ranked · Unrk = vs Unranked · 17+/28+ = win margin bonus tiers. "
        "These are the raw inputs behind the Rating column above -- swipe/scroll the table sideways on mobile to see them all."
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

    st.divider()
    st.subheader("📈 Rating History")
    st.caption("Dynasty Rating (or rank) recomputed as of each completed week, so you can see trajectories over the season.")

    rating_history = dl.compute_rating_history(df, TEAMS, weights)
    if rating_history.empty:
        st.caption("Rating history will appear once at least one week is completed.")
    else:
        hc1, hc2 = st.columns([1, 3])
        with hc1:
            view_mode = st.radio("View", ["Rating", "Rank"], horizontal=True, key="rating_history_view")
        with hc2:
            default_teams = list(ranked_display.sort_values("Rank")["Team"].head(8))
            team_filter_hist = st.multiselect(
                "Teams to show", TEAMS, default=default_teams, format_func=team_display,
                key="rating_history_teams",
            )

        plot_df = rating_history[rating_history["Team"].isin(team_filter_hist)] if team_filter_hist else rating_history
        week_order_sorts = sorted(rating_history[["Week_Sort", "Week"]].drop_duplicates()["Week_Sort"])
        week_label_order = [
            rating_history.loc[rating_history["Week_Sort"] == w, "Week"].iloc[0] for w in week_order_sorts
        ]

        y_col = "Dynasty_Rating" if view_mode == "Rating" else "Rank"
        if plot_df.empty:
            st.caption("Pick at least one team to plot.")
        else:
            fig_hist = px.line(
                plot_df.sort_values("Week_Sort"), x="Week", y=y_col, color="Team",
                category_orders={"Week": week_label_order}, markers=True,
                hover_data={"User": True},
            )
            if view_mode == "Rank":
                fig_hist.update_yaxes(autorange="reversed", dtick=1)
            fig_hist.update_layout(height=500, legend_title_text="")
            st.plotly_chart(fig_hist, use_container_width=True)

        st.divider()
        st.subheader("🏁 Rating Race")
        st.caption("Same data, animated — hit play to watch the field shuffle week by week. Drag the slider to jump to any week.")

        race_weeks = sorted(rating_history["Week_Sort"].unique())
        if len(race_weeks) < 2:
            st.caption("Need at least two completed weeks for the race to animate.")
        else:
            frames = []
            for w in race_weeks:
                wk_data = rating_history[rating_history["Week_Sort"] == w].sort_values("Dynasty_Rating", ascending=True)
                frames.append(go.Frame(
                    data=[go.Bar(
                        x=wk_data["Dynasty_Rating"], y=wk_data["Team"], orientation="h",
                        marker_color=[dl.team_primary_color(t) for t in wk_data["Team"]],
                        text=wk_data["Dynasty_Rating"].round(1), textposition="outside",
                        hovertext=wk_data["User"], hoverinfo="text+x",
                    )],
                    name=str(w),
                    layout=go.Layout(yaxis=dict(categoryorder="array", categoryarray=wk_data["Team"].tolist())),
                ))

            first_wk = rating_history[rating_history["Week_Sort"] == race_weeks[0]].sort_values("Dynasty_Rating", ascending=True)
            race_fig = go.Figure(
                data=[go.Bar(
                    x=first_wk["Dynasty_Rating"], y=first_wk["Team"], orientation="h",
                    marker_color=[dl.team_primary_color(t) for t in first_wk["Team"]],
                    text=first_wk["Dynasty_Rating"].round(1), textposition="outside",
                    hovertext=first_wk["User"], hoverinfo="text+x",
                )],
                frames=frames,
            )
            race_fig.update_layout(
                xaxis=dict(range=[0, 105], title="Dynasty Rating"),
                yaxis=dict(categoryorder="array", categoryarray=first_wk["Team"].tolist(), title="", automargin=True),
                height=max(360, 32 * len(TEAMS)),
                margin=dict(r=10, t=10, b=10),
                updatemenus=[dict(
                    type="buttons", direction="left", x=0, y=1.08, xanchor="left",
                    buttons=[
                        dict(label="▶ Play", method="animate",
                             args=[None, {"frame": {"duration": 700, "redraw": True}, "fromcurrent": True, "transition": {"duration": 300}}]),
                        dict(label="⏸ Pause", method="animate",
                             args=[[None], {"frame": {"duration": 0}, "mode": "immediate"}]),
                    ],
                )],
                sliders=[dict(
                    active=0, x=0, y=-0.02, len=1.0,
                    currentvalue=dict(prefix="Week: "),
                    steps=[
                        dict(label=rating_history.loc[rating_history["Week_Sort"] == w, "Week"].iloc[0],
                             method="animate",
                             args=[[str(w)], {"frame": {"duration": 0, "redraw": True}, "mode": "immediate"}])
                        for w in race_weeks
                    ],
                )],
            )
            st.plotly_chart(race_fig, use_container_width=True)


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
        "Opponent_Rank_Display", "Status", "Outcome", "Team_Score", "Opponent_Score",
    ]].rename(columns={"Opponent_Rank_Display": "Opp Rank", "Opponent_User": "Opponent User"})
    sched_display.insert(2, "Logo", sched_display["Team"].apply(dl.logo_url))
    sched_display.insert(7, "Opp Logo", sched_display["Opponent"].apply(dl.logo_url))
    st.dataframe(
        sched_display, use_container_width=True, hide_index=True, height=650,
        column_config={
            "Logo": st.column_config.ImageColumn("", width="small"),
            "Opp Logo": st.column_config.ImageColumn("", width="small"),
        },
    )


# ============================================================================
# PAGE: TEAMS (report cards)
# ============================================================================
elif page == "👤 Teams":
    st.title("👤 Team Report Card")

    selected_team = st.selectbox("Select a team", TEAMS, format_func=team_display)
    if selected_team in team_stats.index:
        row = team_stats.loc[selected_team]
        rating_row = rated.loc[selected_team] if selected_team in rated.index else None

        primary = dl.team_primary_color(selected_team)

        render_team_hero(
            selected_team, row["User"],
            rank=rating_row["Rank"] if rating_row is not None else None,
            rating=rating_row["Dynasty_Rating"] if rating_row is not None else None,
        )

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

        colored_divider(primary)
        st.subheader("Game Log")
        log = df[df["Team"] == selected_team].sort_values("Week_Sort")
        log_display = log[[
            "Week", "Date", "Location", "Opponent", "Opponent_Rank_Display",
            "Status", "Outcome", "Team_Score", "Opponent_Score",
        ]].rename(columns={"Opponent_Rank_Display": "Opp Rank"})
        log_display.insert(3, "Opp Logo", log_display["Opponent"].apply(dl.logo_url))
        st.dataframe(
            log_display, use_container_width=True, hide_index=True, height=450,
            column_config={"Opp Logo": st.column_config.ImageColumn("", width="small")},
        )

        if rating_row is not None:
            colored_divider(primary)
            st.subheader("Why this ranking?")
            for b in dl.rating_explanation(selected_team, rated):
                st.markdown(f"- {b}")


# ============================================================================
# PAGE: HEAD-TO-HEAD
# ============================================================================
elif page == "🤝 Head-to-Head":
    st.title("🤝 Head-to-Head")
    st.caption(
        "This page covers your ENTIRE dynasty history, every season combined -- not just the season selected "
        "in the sidebar. With only 2-3 User-vs-User games a season, a single-season view didn't have enough "
        "games to be very interesting; across every season, it does."
    )

    h2h_teams = sorted(df_all["Team"].dropna().unique().tolist())
    uu_records = dl.user_vs_user_records(df_all, h2h_teams)

    st.subheader("User vs User Records")
    uu_display = uu_records.copy()
    uu_display["Record"] = uu_display["UU_W"].astype(str) + "-" + uu_display["UU_L"].astype(str)
    uu_display = uu_display.reset_index()
    uu_display["Logo"] = uu_display["Team"].apply(dl.logo_url)
    uu_display = uu_display[["Logo", "Team", "User", "Record", "Wins_Over", "Losses_To"]].sort_values(
        "Record", key=lambda s: s.map(lambda r: -int(r.split("-")[0]))
    )
    st.dataframe(
        uu_display, use_container_width=True, hide_index=True,
        column_config={"Logo": st.column_config.ImageColumn("", width="small")},
    )

    st.divider()
    st.subheader("User vs User Stats")
    st.caption(
        "Scoring specifically in User-vs-User games, across your whole dynasty history. Same "
        "\"value (games)\" format as League Stats -- e.g. \"+10.5 (3)\" means +10.5 points/game average across 3 games."
    )
    uu_splits = dl.compute_split_stats(df_all, h2h_teams)
    uu_splits_display = uu_splits.reset_index()
    uu_splits_display["Logo"] = uu_splits_display["Team"].apply(dl.logo_url)
    uu_splits_display = uu_splits_display.merge(
        uu_display[["Team", "Record"]], on="Team", how="left"
    )

    def _fmt_plain(value, gp):
        return "—" if pd.isna(value) or pd.isna(gp) or gp == 0 else f"{value:.1f} ({int(gp)})"

    def _fmt_signed(value, gp):
        return "—" if pd.isna(value) or pd.isna(gp) or gp == 0 else f"{value:+.1f} ({int(gp)})"

    uu_splits_display["User PPG (n)"] = uu_splits_display.apply(lambda r: _fmt_plain(r["User_PPG"], r["User_GP"]), axis=1)
    uu_splits_display["User PA (n)"] = uu_splits_display.apply(lambda r: _fmt_plain(r["User_PA"], r["User_GP"]), axis=1)
    uu_splits_display["User Margin (n)"] = uu_splits_display.apply(lambda r: _fmt_signed(r["User_Margin"], r["User_GP"]), axis=1)

    uu_stats_table = uu_splits_display[["Logo", "Team", "Record", "User PPG (n)", "User PA (n)", "User Margin (n)"]].rename(
        columns={"Logo": ""}
    )
    st.dataframe(
        uu_stats_table, use_container_width=True, hide_index=True,
        column_config={"": st.column_config.ImageColumn("", width="small")},
    )

    user_games_only = df_all[df_all["Opponent_Is_User"]]
    biggest_uu = dl.biggest_blowout(user_games_only)
    closest_uu = dl.closest_game(user_games_only)

    uc1, uc2 = st.columns(2)
    with uc1:
        st.markdown("#### Biggest User Blowout")
        if biggest_uu:
            season_tag = f" ({int(biggest_uu['season'])})" if pd.notna(biggest_uu.get("season")) else ""
            st.markdown(
                f"**{biggest_uu['score']}**{season_tag} — {team_logo_tag(biggest_uu['winner'], 24)}{biggest_uu['winner']} vs "
                f"{team_logo_tag(biggest_uu['loser'], 24)}{biggest_uu['loser']}",
                unsafe_allow_html=True,
            )
        else:
            st.caption("No User-vs-User games yet")
    with uc2:
        st.markdown("#### Closest User Game")
        if closest_uu:
            season_tag = f" ({int(closest_uu['season'])})" if pd.notna(closest_uu.get("season")) else ""
            st.markdown(
                f"**{closest_uu['score']}**{season_tag} — {team_logo_tag(closest_uu['winner'], 24)}{closest_uu['winner']} over "
                f"{team_logo_tag(closest_uu['loser'], 24)}{closest_uu['loser']}",
                unsafe_allow_html=True,
            )
        else:
            st.caption("No User-vs-User games yet")

    st.divider()
    st.subheader("League Matrix")
    st.caption(
        "Row team's result vs. column team (W / L / — no game yet), across every season. "
        "Note: if a team's changed hands between seasons, this shows the CURRENT controller's name for "
        "every historical game on that team, not necessarily whoever was actually playing at the time."
    )

    h2h_matrix_all = dl.build_h2h_matrix(df_all, h2h_teams)
    team_user_map_all = df_all.drop_duplicates("Team").set_index("Team")["User"].to_dict()
    matrix_labels = {t: (f"{t} ({team_user_map_all[t]})" if team_user_map_all.get(t) else t) for t in h2h_teams}
    matrix_display = h2h_matrix_all.rename(index=matrix_labels, columns=matrix_labels)

    def _color_cell(val):
        if val in ("-", "—") or not val:
            return ""
        entries = [e.strip() for e in val.split(",")]
        has_w = any(e.startswith("W") for e in entries)
        has_l = any(e.startswith("L") for e in entries)
        if has_w and has_l:
            return "background-color: rgba(241, 196, 15, 0.25)"  # split record across seasons
        if has_w:
            return "background-color: rgba(46, 204, 113, 0.25)"
        if has_l:
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

    season_scope = st.radio(
        "Show stats for:", ["Current Season", "All Seasons"], horizontal=True,
        help="\"All Seasons\" combines every season's games together -- useful once you have more than one season loaded.",
    )
    if season_scope == "All Seasons":
        ls_df = df_all
        ls_teams = sorted(df_all["Team"].dropna().unique().tolist())
    else:
        ls_df = df
        ls_teams = TEAMS
    ls_team_stats = dl.compute_team_stats(ls_df, ls_teams)
    ls_team_stats = dl.add_strength_of_schedule(ls_df, ls_team_stats)

    st.subheader("Full League Table")
    league_table = dl.compute_league_stats_table(ls_df, ls_teams, ls_team_stats).reset_index()
    league_table["Record"] = (
        league_table["W"].fillna(0).astype(int).astype(str) + "-" + league_table["L"].fillna(0).astype(int).astype(str)
    )
    league_table["One-Score Rec"] = (
        league_table["One_Score_W"].fillna(0).astype(int).astype(str) + "-"
        + league_table["One_Score_L"].fillna(0).astype(int).astype(str)
    )
    league_table["Logo"] = league_table["Team"].apply(dl.logo_url)
    league_table["Win %"] = (league_table["Win_Pct"] * 100).round(1)
    league_table["PPG"] = league_table["PF"].round(1)
    league_table["PA/G"] = league_table["PA"].round(1)
    league_table["Avg Margin"] = league_table["MOV"].round(1)
    league_table["SOS"] = league_table["SOS"].round(3)

    display_cols = {
        "Logo": "", "Team": "Team", "Record": "Record", "Win %": "Win %",
        "PPG": "PPG", "PA/G": "PA/G", "Avg Margin": "Avg Margin",
        "Total_PF": "Total PF", "Total_PA": "Total PA",
        "Best_Game_PF": "Best Game", "Worst_Game_PA": "Worst Def Game",
        "Biggest_Win_Margin": "Biggest Win", "Worst_Loss_Margin": "Worst Loss",
        "One-Score Rec": "One-Score Rec", "Longest_Win_Streak": "Longest W Streak",
        "Longest_Loss_Streak": "Longest L Streak", "SOS": "SOS",
        "Win_Quality_Score": "Win Quality",
    }
    league_display = league_table[list(display_cols.keys())].rename(columns=display_cols)
    st.dataframe(
        league_display, use_container_width=True, hide_index=True,
        height=min(35 * (len(league_display) + 1) + 3, 640),
        column_config={"": st.column_config.ImageColumn("", width="small")},
    )
    st.caption(
        "PPG, PA/G, and Avg Margin are per-game averages, not totals (Total PF/PA alongside them are the actual "
        "totals, for reference). Win Quality = sum of (26 − opponent rank) across all ranked wins, same scoring "
        "the Power Rankings formula uses. Swipe/scroll sideways on mobile to see every column."
    )

    st.divider()

    best_off = ls_team_stats["PF"].idxmax() if ls_team_stats["PF"].notna().any() else None
    best_def = ls_team_stats["PA"].idxmin() if ls_team_stats["PA"].notna().any() else None
    blowout = dl.biggest_blowout(ls_df)
    closest = dl.closest_game(ls_df)
    hfa = dl.home_field_advantage(ls_df)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### Best Offense")
        if best_off:
            st.markdown(f"{team_logo_tag(best_off, 28)}**{best_off}** — {ls_team_stats.loc[best_off, 'PF']:.1f} PPG", unsafe_allow_html=True)
        else:
            st.caption("No data yet")
    with c2:
        st.markdown("#### Best Defense")
        if best_def:
            st.markdown(f"{team_logo_tag(best_def, 28)}**{best_def}** — {ls_team_stats.loc[best_def, 'PA']:.1f} PA/G", unsafe_allow_html=True)
        else:
            st.caption("No data yet")

    c3, c4 = st.columns(2)
    with c3:
        st.markdown("#### Biggest Blowout")
        if blowout:
            st.markdown(
                f"**{blowout['score']}** — {team_logo_tag(blowout['winner'], 24)}{blowout['winner']} vs "
                f"{team_logo_tag(blowout['loser'], 24)}{blowout['loser']}",
                unsafe_allow_html=True,
            )
        else:
            st.caption("No data yet")
    with c4:
        st.markdown("#### Closest Game")
        if closest:
            st.markdown(
                f"**{closest['score']}** — {team_logo_tag(closest['winner'], 24)}{closest['winner']} over "
                f"{team_logo_tag(closest['loser'], 24)}{closest['loser']}",
                unsafe_allow_html=True,
            )
        else:
            st.caption("No data yet")

    sig_win = dl.signature_win(ls_df)
    bad_loss = dl.worst_loss(ls_df)

    c7, c8 = st.columns(2)
    with c7:
        st.markdown("#### Signature Win")
        if sig_win:
            st.markdown(
                f"**{sig_win['score']}** — {team_logo_tag(sig_win['team'], 24)}{sig_win['team']} over "
                f"#{sig_win['opponent_rank']} {sig_win['opponent']}",
                unsafe_allow_html=True,
            )
        else:
            st.caption("No wins over a ranked opponent yet")
    with c8:
        st.markdown("#### Worst Loss")
        if bad_loss:
            st.markdown(
                f"**{bad_loss['score']}** — {team_logo_tag(bad_loss['team'], 24)}{bad_loss['team']} fell to "
                f"unranked {bad_loss['opponent']}",
                unsafe_allow_html=True,
            )
        else:
            st.caption("No bad losses yet -- clean season so far")

    st.divider()
    st.subheader("Advanced Splits")
    st.caption(
        "Every stat below is a per-game AVERAGE for that specific context, never a total. The number in "
        "parentheses is how many games that average is based on -- e.g. \"+10.5 (3)\" means +10.5 points/game "
        "average, across 3 games. Blank means zero games in that context so far."
    )
    split_stats = dl.compute_split_stats(ls_df, ls_teams)
    split_display = split_stats.reset_index()
    split_display["Logo"] = split_display["Team"].apply(dl.logo_url)

    def _fmt_with_gp(value, gp):
        if pd.isna(value) or pd.isna(gp) or gp == 0:
            return "—"
        return f"{value:+.1f} ({int(gp)})" if value == value else "—"

    def _fmt_plain_with_gp(value, gp):
        if pd.isna(value) or pd.isna(gp) or gp == 0:
            return "—"
        return f"{value:.1f} ({int(gp)})"

    split_display["Avg Margin, Wins (n)"] = split_display.apply(lambda r: _fmt_with_gp(r["Margin_Wins"], r["Margin_Wins_GP"]), axis=1)
    split_display["Avg Margin, Losses (n)"] = split_display.apply(lambda r: _fmt_with_gp(r["Margin_Losses"], r["Margin_Losses_GP"]), axis=1)
    split_display["Home PPG (n)"] = split_display.apply(lambda r: _fmt_plain_with_gp(r["Home_PPG"], r["Home_GP"]), axis=1)
    split_display["Home PA (n)"] = split_display.apply(lambda r: _fmt_plain_with_gp(r["Home_PA"], r["Home_GP"]), axis=1)
    split_display["Home Margin (n)"] = split_display.apply(lambda r: _fmt_with_gp(r["Home_Margin"], r["Home_GP"]), axis=1)
    split_display["Away PPG (n)"] = split_display.apply(lambda r: _fmt_plain_with_gp(r["Away_PPG"], r["Away_GP"]), axis=1)
    split_display["Away PA (n)"] = split_display.apply(lambda r: _fmt_plain_with_gp(r["Away_PA"], r["Away_GP"]), axis=1)
    split_display["Away Margin (n)"] = split_display.apply(lambda r: _fmt_with_gp(r["Away_Margin"], r["Away_GP"]), axis=1)
    split_display["User-Game Margin (n)"] = split_display.apply(lambda r: _fmt_with_gp(r["User_Margin"], r["User_GP"]), axis=1)
    split_display["CPU-Game Margin (n)"] = split_display.apply(lambda r: _fmt_with_gp(r["CPU_Margin"], r["CPU_GP"]), axis=1)
    split_display["Avg Margin vs Ranked (n)"] = split_display.apply(lambda r: _fmt_with_gp(r["Margin_vs_Ranked"], r["Ranked_GP"]), axis=1)
    split_display["Avg Margin vs Unranked (n)"] = split_display.apply(lambda r: _fmt_with_gp(r["Margin_vs_Unranked"], r["Unranked_GP"]), axis=1)
    split_display["Blowout % (of GP)"] = split_display.apply(
        lambda r: f"{r['Blowout_Rate']:.0f}% ({int(r['Total_GP'])})" if pd.notna(r["Blowout_Rate"]) and r["Total_GP"] else "—", axis=1)
    split_display["One-Score % (of GP)"] = split_display.apply(
        lambda r: f"{r['OneScore_Rate']:.0f}% ({int(r['Total_GP'])})" if pd.notna(r["OneScore_Rate"]) and r["Total_GP"] else "—", axis=1)

    split_table = split_display[[
        "Logo", "Team", "Avg Margin, Wins (n)", "Avg Margin, Losses (n)",
        "Home PPG (n)", "Home PA (n)", "Home Margin (n)",
        "Away PPG (n)", "Away PA (n)", "Away Margin (n)",
        "User-Game Margin (n)", "CPU-Game Margin (n)",
        "Avg Margin vs Ranked (n)", "Avg Margin vs Unranked (n)",
        "Blowout % (of GP)", "One-Score % (of GP)",
    ]].rename(columns={"Logo": ""})
    st.dataframe(
        split_table, use_container_width=True, hide_index=True,
        height=min(35 * (len(split_table) + 1) + 3, 640),
        column_config={"": st.column_config.ImageColumn("", width="small")},
    )
    st.caption("Swipe/scroll sideways on mobile to see every column.")

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
    scatter_df = ls_team_stats.reset_index().dropna(subset=["PF", "PA"])
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

        recap = dl.weekly_recap(df, rated, sel_week_sort, rank_basis=PRIMARY_RANK_BASIS)
        if not recap:
            st.caption("No completed games that week.")
        else:
            st.subheader(f"Week {week_labels[sel_week_sort]}")

            with st.container(border=True):
                st.markdown("##### 🎮 Game of the Week")
                g = recap["game_of_week"]
                st.markdown(
                    f"{team_logo_tag(g['winner'], 26)}**{g['winner']}** def. "
                    f"{team_logo_tag(g['loser'], 26)}**{g['loser']}**, {g['score']}",
                    unsafe_allow_html=True,
                )

            if recap["upset"]:
                with st.container(border=True):
                    st.markdown("##### 😱 Upset Alert")
                    st.markdown(recap["upset"])

            with st.container(border=True):
                st.markdown("##### 🔢 Highest Scoring")
                hs = recap["highest_scoring"]
                st.markdown(
                    f"{team_logo_tag(hs['team_a'], 26)}**{hs['team_a']}** {hs['score_a']:.0f} — "
                    f"{hs['score_b']:.0f} **{hs['team_b']}**{team_logo_tag(hs['team_b'], 26)}",
                    unsafe_allow_html=True,
                )

            with st.container(border=True):
                st.markdown("##### 🛡️ Defensive Performance")
                bd = recap["best_defense"]
                st.markdown(
                    f"{team_logo_tag(bd['team'], 26)}**{bd['team']}** held "
                    f"{team_logo_tag(bd['opponent'], 26)}**{bd['opponent']}** to {bd['points_allowed']:.0f} points",
                    unsafe_allow_html=True,
                )

            st.divider()
            st.subheader("📋 Shareable Recap")
            st.caption("Copy-paste ready for your group chat / Discord — use the copy icon in the top-right of the box.")
            recap_text = dl.format_weekly_recap_text(recap, week_labels[sel_week_sort])
            st.code(recap_text, language=None)


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

    cards_html = []
    for key, title, subtitle, fmt in cards:
        if key in fs:
            logo = team_logo_tag(fs[key]["team"], 26)
            value_html = f'<div style="font-size:1.3rem; font-weight:700; margin-top:4px;">{logo}{fmt(fs[key])}</div>'
        else:
            value_html = '<div class="stat-label" style="margin-top:4px;">Not enough data yet</div>'
        cards_html.append(
            '<div style="flex:1 1 300px; border:1px solid #2a2f3a; border-radius:10px; padding:14px 16px;">'
            f'<div style="font-weight:700;">{title}</div>'
            f'<div class="stat-label">{subtitle}</div>'
            f'{value_html}'
            '</div>'
        )
    # Flexbox with wrap, not st.columns() -- keeps cards in this exact
    # curated order regardless of screen width, instead of scrambling into
    # column-then-column blocks once columns stack on a narrow/mobile screen.
    st.markdown(
        f'<div style="display:flex; flex-wrap:wrap; gap:12px;">{"".join(cards_html)}</div>',
        unsafe_allow_html=True,
    )


# ============================================================================
# PAGE: CAREER (multi-season)
# ============================================================================
elif page == "📜 Career":
    st.title("📜 Career Stats")

    if len(seasons_available) <= 1:
        st.info(
            "Only one season is currently loaded, so career stats match this "
            "season's numbers. This page comes into its own once more season "
            "files are dropped in the folder (see ⚙️ Settings for details)."
        )
    st.caption(
        "Aggregated by **coach (user)** rather than by team, since who "
        "controls a given team can change season to season. "
        f"Seasons loaded: {', '.join(str(int(s)) for s in sorted(seasons_available))}."
    )

    multi_hist = dl.compute_multi_season_rating_history(df_all, weights)
    career = dl.compute_career_stats(df_all, multi_hist)

    st.subheader("Career Standings")
    career_display = career.reset_index()
    career_display["Record"] = (
        career_display["Career_W"].astype(int).astype(str) + "-" + career_display["Career_L"].astype(int).astype(str)
    )
    career_display["UU Record"] = (
        career_display["UU_W"].astype(int).astype(str) + "-" + career_display["UU_L"].astype(int).astype(str)
    )
    career_display["Win %"] = (career_display["Win_Pct"] * 100).round(1)
    if "Best_Season" in career_display.columns:
        career_display["Best Season"] = career_display.apply(
            lambda r: (
                f"{int(r['Best_Season'])} ({r['Best_Season_Team']}, {r['Best_Season_Rating']:.1f})"
                if pd.notna(r.get("Best_Season")) else "—"
            ),
            axis=1,
        )
    else:
        career_display["Best Season"] = "—"

    show_cols = ["User", "Seasons_Played", "Teams_By_Season", "Record", "Win %",
                 "Ranked_Wins", "UU Record", "Best Season"]
    career_display = career_display[show_cols].rename(columns={
        "Seasons_Played": "Seasons", "Teams_By_Season": "Teams By Season", "Ranked_Wins": "Ranked Wins",
    })
    st.dataframe(career_display, use_container_width=True, hide_index=True, height=500)

    st.divider()
    st.subheader("📈 Career Rating History")
    st.caption("Dynasty Rating recomputed fresh within each season, stitched into one continuous career timeline per coach.")

    if multi_hist.empty:
        st.caption("Not enough completed games yet.")
    else:
        all_users = sorted(multi_hist["User"].dropna().unique())
        default_users = list(career.sort_values("Win_Pct", ascending=False).index[:8])
        chosen_users = st.multiselect("Coaches to show", all_users, default=default_users, key="career_users")

        plot_hist = multi_hist[multi_hist["User"].isin(chosen_users)] if chosen_users else multi_hist
        if plot_hist.empty:
            st.caption("Pick at least one coach to plot.")
        else:
            label_order = (
                multi_hist[["Global_Order", "Season_Week_Label"]]
                .drop_duplicates()
                .sort_values("Global_Order")["Season_Week_Label"]
                .tolist()
            )
            fig_career = px.line(
                plot_hist.sort_values("Global_Order"), x="Season_Week_Label", y="Dynasty_Rating",
                color="User", markers=True, hover_data={"Team": True, "Season": True},
                category_orders={"Season_Week_Label": label_order},
                labels={"Season_Week_Label": "Season / Week"},
            )
            fig_career.update_layout(height=520, legend_title_text="")
            st.plotly_chart(fig_career, use_container_width=True)


# ============================================================================
# PAGE: SETTINGS
# ============================================================================
elif page == "⚙️ Settings":
    st.title("⚙️ Settings")

    st.subheader("Dynasty Rating Weights")
    st.caption("These control the Power Rankings formula. They're re-normalized automatically so they always sum to 100%.")

    w = st.session_state["rating_weights"]
    labels = {
        "user_wins": "1. User-vs-User Wins",
        "road_ranked_wins": "2. Road Wins vs Ranked",
        "home_ranked_wins": "3. Home Wins vs Ranked",
        "road_ranked_wins_big": "4. Road Wins vs Ranked by 17+",
        "home_ranked_wins_big": "5. Home Wins vs Ranked by 17+",
        "road_unranked_wins": "6. Road Wins vs Unranked",
        "home_unranked_wins": "7. Home Wins vs Unranked",
        "road_unranked_wins_big": "8. Road Wins vs Unranked by 28+",
        "home_unranked_wins_big": "9. Home Wins vs Unranked by 28+",
        "win_pct": "Overall Win % (baseline)",
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
    st.subheader("Data Source & Seasons")
    if using_uploads:
        st.info(f"Using {len(loaded_file_labels)} uploaded file(s) for this session: " + ", ".join(loaded_file_labels))
    elif loaded_file_labels:
        st.info(f"Auto-loaded from this folder: {', '.join(loaded_file_labels)}")
    else:
        st.warning("No season files currently loaded.")
    st.caption(
        f"Seasons detected: {', '.join(str(int(s)) for s in sorted(seasons_available))}  ·  "
        f"Currently viewing: **{int(selected_season)}** (switch seasons from the sidebar)"
    )
    st.caption(
        "To add a new season: drop a file named like `dynasty_data_2027.csv` in this "
        "folder and click \"🔄 Reload data files\" in the sidebar (or restart the app)."
    )
    st.caption(f"Last export timestamp in current season's file: {df['Last_Updated'].iloc[0] if 'Last_Updated' in df.columns and len(df) else '—'}")

    st.divider()
    st.subheader("About the Dynasty Rating")
    st.markdown("""
    - **1. User-vs-User Wins**: wins over other human-controlled teams -- the single biggest factor
    - **2-3. Road/Home Wins vs Ranked**: wins over AP-ranked opponents, with road wins weighted
      above home wins. Beating a higher-ranked opponent counts for more than beating a lower-ranked
      one (e.g. beating #3 contributes far more than beating #24)
    - **4-5. ...by 17+ points**: the same two categories again, but only for wins by a big margin
    - **6-7. Road/Home Wins vs Unranked**: wins over unranked opponents, road weighted above home
    - **8-9. ...by 28+ points**: the same two categories again, for big blowout margins
    - **Overall Win % (baseline)**: season winning percentage, so a team's whole record still
      counts for something even without a marquee win

    Each component is scaled 0-100 relative to the rest of the league, then combined
    using the weights above.
    """)


# ============================================================================
# GLOBAL FOOTER (renders after whichever page ran above, on every page)
# ============================================================================
st.markdown(
    '<div class="footer-note">'
    '🔒 <b>Opponent Rank</b> freezes the moment a game is marked Completed — a win over the '
    '#1 team stays a win over the #1 team, even if that opponent later slips in the polls. '
    'Games that haven\'t been played yet still show their current live rank, which updates '
    'as CPU results come in.'
    '</div>',
    unsafe_allow_html=True,
)
