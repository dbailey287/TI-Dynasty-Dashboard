"""
Core logic for the Ready-To-Advance (RTA) Discord tracker.

Kept separate from the actual Discord-polling script so the message
matching and state-update logic can be unit tested without needing a
live Discord connection or GitHub Actions environment.

Two channels are involved:
  - The MAIN chat channel (#26-mega-dynasty): scanned for the standalone
    word "RTA" from any ACTIVE roster member, matched by Discord user ID
    (not by name -- names change, IDs don't, and this is also just more
    robust than string-matching usernames/nicknames).
  - An ADMIN-ONLY channel (#monitored-admin-advance): scanned for the
    word "advance"/"advanced". Authorization is handled by Discord itself
    here (only admins can post in that channel), not by checking who sent
    the message.

Roster (team / username / display_name / user_id) comes from roster.py,
which reads Server_Members_Teams.csv -- not from dynasty_data_<season>.csv,
since that file was never a reliable source of Discord identity (see
roster.py's docstring for the full story).
"""
import glob
import json
import os
import random
import re
from datetime import datetime, timezone

import roster as _roster

# Matches the standalone word "RTA" (case-insensitive), not a substring
# inside a longer word (e.g. won't fire on "mortar").
RTA_PATTERN = re.compile(r"\brta\b", re.IGNORECASE)

# Signals this is a QUESTION or DISCUSSION about RTA status, not someone
# declaring themselves ready -- e.g. "who's not RTA?" or "is everyone RTA
# yet?" should never mark the asker as ready.
_DISCUSSION_SIGNAL = re.compile(
    r"\?|\b(who|whos|who's|anyone|everyone|status|list)\b",
    re.IGNORECASE,
)

# "not RTA", "isn't RTA", "RTA... not yet" etc -- a negated mention should
# never count as a positive declaration. Looks up to ~20 characters on
# either side of "rta" for a negation word, stopping at sentence
# punctuation so it doesn't reach into an unrelated clause.
_NEGATION_SIGNAL = re.compile(
    r"\b(not|isn'?t|ain'?t|aren'?t|no)\b[^.!?]{0,20}\brta\b"
    r"|\brta\b[^.!?]{0,20}\b(not|isn'?t|ain'?t|aren'?t|no)\b",
    re.IGNORECASE,
)

DEFAULT_ADVANCE_KEYWORD = "advance"

FUNNY_ADVANCE_MESSAGES = [
    "🚀 The commish has spoken. **Week has advanced** — go check your new matchups!",
    "⏭️ Advance detected. May your rankings be favorable and your RNG merciful.",
    "📢 The wheel has turned. A new week dawns. Choose your opponent wisely.",
    "🐐 Somewhere, a coordinator just lost their job. The week has advanced.",
    "⚡ It is Simulated. The season marches on — new week is live.",
    "🏈 Advance called. Time to find out who's actually good.",
    "🔔 Ding ding ding — new week, new week, new week!",
    "🎬 And... cut. That's a wrap on last week. Next!",
    "🚨 PSA: the byes are over for some of you. Good luck.",
    "🌀 The dynasty spins forward. Week advanced.",
    "🧙 By the power vested in the commissioner, this week is complete.",
    "📅 Flip the calendar. It's a new week in the dynasty.",
    "🥁 Drumroll please... the week has officially advanced.",
    "🏁 That's the checkered flag on last week. Green flag's out for the next one.",
]

FUNNY_RTA_NAG_MESSAGES = [
    "⏰ Tick tock, the dynasty waits for no one.",
    "👀 Someone's holding up the whole league right now...",
    "📋 Roll call! We're still missing a few RTAs.",
    "🐢 Slow and steady doesn't win in this dynasty.",
    "🚦 We're stuck at a red light waiting on a few folks.",
    "🛑 The advance train is idling, waiting on you.",
    "🎯 So close to advancing... just need a few more RTAs.",
]


def pick_announcement() -> str:
    return random.choice(FUNNY_ADVANCE_MESSAGES)


def pick_nag_intro() -> str:
    return random.choice(FUNNY_RTA_NAG_MESSAGES)


def load_active_roster(directory: str = ".") -> list:
    """Returns the active roster entries (team/username/display_name/user_id)
    from Server_Members_Teams.csv via roster.py. Empty list if not found."""
    path = _roster.find_roster_csv(directory)
    if not path:
        return []
    return _roster.load_roster(path)


def is_rta_message(content: str) -> bool:
    content = content or ""
    if not RTA_PATTERN.search(content):
        return False
    if _DISCUSSION_SIGNAL.search(content):
        return False
    if _NEGATION_SIGNAL.search(content):
        return False
    return True


def is_advance_message(content: str, keyword: str = DEFAULT_ADVANCE_KEYWORD) -> bool:
    """Word-boundary match at the START of the keyword only (so "advance",
    "advanced", "advances" all match) -- not requiring a boundary at the
    end too, since that would incorrectly reject "advanced". Safe to be a
    bit loose here since this only ever scans the admin-only channel."""
    pattern = re.compile(r"\b" + re.escape(keyword), re.IGNORECASE)
    return bool(pattern.search(content or ""))


DEFAULT_STATE = {
    "ready_user_ids": [],
    "last_message_id_main": None,
    "last_message_id_admin": None,
    "last_updated": None,
    "last_reset_at": None,
    "cycle_count": 0,
    "current_week_sort": None,  # explicitly tracked, incremented on each advance -- see check_rta_status.py
}


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return dict(DEFAULT_STATE)
    with open(path, "r") as f:
        state = json.load(f)
    for key, default in DEFAULT_STATE.items():
        state.setdefault(key, default)
    return state


def save_state(path: str, state: dict):
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def process_admin_messages(messages: list, keyword: str, state: dict) -> tuple:
    """
    messages: list of dicts with {"id": str, "content": str}, from the
    ADMIN-ONLY channel, oldest-first.

    Returns (updated_state, advance_triggered).
    """
    last_id = state.get("last_message_id_admin")
    triggered = False

    for msg in messages:
        if is_advance_message(msg["content"], keyword):
            state["ready_user_ids"] = []
            state["cycle_count"] = state.get("cycle_count", 0) + 1
            state["last_reset_at"] = datetime.now(timezone.utc).isoformat()
            triggered = True
        last_id = msg["id"]

    state["last_message_id_admin"] = last_id
    return state, triggered


def process_rta_messages(messages: list, tracked_user_ids: set, state: dict) -> dict:
    """messages: list of dicts with {"id": str, "author_id": str, "content": str},
    from the MAIN chat channel, oldest-first. Only messages from a
    tracked_user_ids member count -- matched by Discord user ID, not name."""
    ready = set(state.get("ready_user_ids", []))
    last_id = state.get("last_message_id_main")

    for msg in messages:
        if msg["author_id"] in tracked_user_ids and is_rta_message(msg["content"]):
            ready.add(msg["author_id"])
        last_id = msg["id"]

    state["ready_user_ids"] = sorted(ready)
    state["last_message_id_main"] = last_id
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    return state


def format_matchup_lines(user_ids: list, id_to_team: dict, matchups: dict) -> list:
    """
    Builds one formatted line per user ID, showing their current-week
    matchup and flagging User vs User games specially. Shared between the
    RTA reminder ("who's not ready, and who are they playing") and the
    advance announcement ("here's everyone's new matchup").
    """
    def sort_key(uid):
        return id_to_team.get(uid) or uid

    lines = []
    for uid in sorted(user_ids, key=sort_key):
        team = id_to_team.get(uid)
        mention = f"<@{uid}>"
        matchup = matchups.get(team) if team else None
        if matchup and matchup.get("is_bye"):
            lines.append(f"😴 {mention} — {team} — **BYE week**")
        elif matchup and matchup["opponent"] != "?":
            opp = matchup["opponent"]
            if matchup["opponent_is_user"]:
                lines.append(f"🔥 {mention} — **{team}** vs **{opp}** — USER MATCHUP!")
            else:
                lines.append(f"- {mention} — {team} vs {opp}")
        elif team:
            lines.append(f"- {mention} — {team}")
        else:
            lines.append(f"- {mention}")
    return lines


def _load_latest_season_df(directory: str):
    """Internal: loads the newest dynasty_data_<season>.csv as a
    dataframe, or None if it can't."""
    import pandas as pd
    files = glob.glob(os.path.join(directory, "dynasty_data_*.csv"))
    if not files:
        return None

    def season_num(path):
        m = re.search(r"dynasty_data_(\d+)\.csv$", os.path.basename(path))
        return int(m.group(1)) if m else -1
    latest = max(files, key=season_num)
    try:
        return pd.read_csv(latest, engine="python", on_bad_lines="skip", dtype=str)
    except Exception:
        return None


def week_sort_key(week_val) -> int:
    w = str(week_val).strip()
    if w.isdigit():
        return int(w)
    if "conf" in w.lower():
        return 900
    return 999


def find_earliest_upcoming_week_sort(directory: str = ".") -> int:
    """
    Best-effort "what week is it right now" -- the earliest week where any
    team's game is still marked Upcoming. This is what powers the RTA
    reminder (fine there, since it's read-only and self-corrects every
    run) but is ONLY used to bootstrap the advance tracker's week counter
    the very first time it's ever used -- see get_matchups_for_week_sort
    and check_rta_status.py's advance handling for why it's not used on
    every advance. Returns None if it can't determine anything.
    """
    df = _load_latest_season_df(directory)
    if df is None:
        return None
    upcoming = df[df["Status"] == "Upcoming"]
    if upcoming.empty:
        return None
    return int(upcoming["Week"].apply(week_sort_key).min())


def get_week_label_for_sort(directory: str, week_sort: int) -> str:
    """Looks up the human-readable Week label (e.g. "3", "Conf Champ")
    for a given week_sort value, from ANY row regardless of Upcoming/
    Completed status -- the full season's weeks all exist in the CSV
    from the first-ever scrape, so this works even if that particular
    week's games haven't been played/scraped yet. Falls back to the raw
    number as a string if nothing matches."""
    df = _load_latest_season_df(directory)
    if df is None:
        return str(week_sort)
    matches = df[df["Week"].apply(week_sort_key) == week_sort]
    if matches.empty:
        return str(week_sort)
    return str(matches.iloc[0]["Week"])


def get_matchups_for_week_sort(directory: str, week_sort: int) -> dict:
    """Who's playing whom for an EXPLICIT week_sort -- not derived from
    which games happen to still say Upcoming, so this stays correct even
    if the scraper hasn't caught up on some teams yet. Returns
    {team: {"opponent": str, "opponent_is_user": bool, "is_bye": bool}},
    or {} if nothing's found. BYE weeks are included and flagged rather
    than silently dropped -- a team on a bye still gets tagged, just with
    "BYE week" shown instead of an opponent."""
    df = _load_latest_season_df(directory)
    if df is None:
        return {}
    week_games = df[
        (df["Week"].apply(week_sort_key) == week_sort)
        & (df["Status"].isin(["Upcoming", "Completed", "BYE"]))
    ]
    matchups = {}
    for _, row in week_games.iterrows():
        status = (row.get("Status") or "").strip()
        if status == "BYE":
            matchups[row["Team"]] = {"opponent": None, "opponent_is_user": False, "is_bye": True}
            continue
        opponent_user = (row.get("Opponent_User") or "CPU").strip()
        matchups[row["Team"]] = {
            "opponent": (row.get("Opponent") or "?").strip(),
            "opponent_is_user": opponent_user != "CPU",
            "is_bye": False,
        }
    return matchups


def get_current_week_matchups(directory: str = ".") -> tuple:
    """
    Best-effort "what week is it, and who's playing whom" -- used by the
    RTA reminder, which re-derives this fresh every time it runs (fine
    there, since it's read-only display info that self-corrects). Returns
    (week_label, {team: {...}}), or ("?", {}) if nothing can be determined.

    NOTE: the advance announcement does NOT use this function -- it tracks
    its own week number explicitly (see check_rta_status.py) specifically
    to avoid the staleness problem this function is inherently subject to
    (if even one team's data hasn't been scraped yet, this can report an
    already-finished week as "current").
    """
    week_sort = find_earliest_upcoming_week_sort(directory)
    if week_sort is None:
        return "?", {}
    week_label = get_week_label_for_sort(directory, week_sort)
    matchups = get_matchups_for_week_sort(directory, week_sort)
    return week_label, matchups


def parse_channel_ids(raw: str) -> list:
    """Splits a comma-separated (whitespace-tolerant) list of channel IDs,
    e.g. "111,222" or "111, 222" -> ["111", "222"]. A single ID with no
    comma just returns a one-item list, so this is safe to use everywhere
    a channel ID env var is read, whether or not multiple were given."""
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]
