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


def get_current_week_matchups(directory: str = ".") -> tuple:
    """
    Best-effort lookup of "who is each team playing this week," for the
    reminder message (so it can show "Arkansas vs Missouri" next to
    someone's name, and flag User vs User games specially). Returns
    (week_label, {team: {"opponent": str, "opponent_is_user": bool}}).
    Returns ("?", {}) on anything unexpected -- this is an enrichment,
    never something that should block the actual reminder from going out.
    """
    import pandas as pd

    files = glob.glob(os.path.join(directory, "dynasty_data_*.csv"))
    if not files:
        return "?", {}

    def season_num(path):
        m = re.search(r"dynasty_data_(\d+)\.csv$", os.path.basename(path))
        return int(m.group(1)) if m else -1
    latest = max(files, key=season_num)

    try:
        df = pd.read_csv(latest, engine="python", on_bad_lines="skip", dtype=str)
        upcoming = df[df["Status"] == "Upcoming"]
        if upcoming.empty:
            return "?", {}

        def week_sort_key(w):
            w = str(w).strip()
            if w.isdigit():
                return int(w)
            if "conf" in w.lower():
                return 900
            return 999
        upcoming = upcoming.copy()
        upcoming["_sort"] = upcoming["Week"].apply(week_sort_key)
        week_label = str(upcoming.loc[upcoming["_sort"].idxmin(), "Week"])

        week_games = df[(df["Week"] == week_label) & (df["Status"].isin(["Upcoming", "Completed"]))]
        matchups = {}
        for _, row in week_games.iterrows():
            opponent_user = (row.get("Opponent_User") or "CPU").strip()
            matchups[row["Team"]] = {
                "opponent": (row.get("Opponent") or "?").strip(),
                "opponent_is_user": opponent_user != "CPU",
            }
        return week_label, matchups
    except Exception:
        return "?", {}


def find_current_week_label(directory: str = ".") -> str:
    """
    Best-effort lookup of "what week is it" from the newest
    dynasty_data_<season>.csv, for the advance announcement ("we've
    advanced to Week X"). Uses the same "earliest week with an Upcoming
    game" logic the dashboard uses. Returns "?" if it can't determine one
    (missing file, no Upcoming games left, etc.) rather than raising --
    this is a nice-to-have detail in a Discord message, not something
    that should ever crash the actual RTA reset.
    """
    import pandas as pd

    files = glob.glob(os.path.join(directory, "dynasty_data_*.csv"))
    if not files:
        return "?"

    def season_num(path):
        m = re.search(r"dynasty_data_(\d+)\.csv$", os.path.basename(path))
        return int(m.group(1)) if m else -1
    latest = max(files, key=season_num)

    try:
        df = pd.read_csv(latest, engine="python", on_bad_lines="skip", dtype=str)
        upcoming = df[df["Status"] == "Upcoming"]
        if upcoming.empty:
            return "?"

        def week_sort_key(w):
            w = str(w).strip()
            if w.isdigit():
                return int(w)
            if "conf" in w.lower():
                return 900
            return 999
        upcoming = upcoming.copy()
        upcoming["_sort"] = upcoming["Week"].apply(week_sort_key)
        return str(upcoming.loc[upcoming["_sort"].idxmin(), "Week"])
    except Exception:
        return "?"
