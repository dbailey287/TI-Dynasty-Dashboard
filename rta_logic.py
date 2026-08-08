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

# Short, standalone fan chants -- NOT full fight song lyrics (some of
# those are copyrighted), just the well-known 2-6 word rallying cries
# fans actually shout. Add more anytime; it's just a plain list per team.
TEAM_TAGLINES = {
    "Arizona State": [
        "Fork 'Em, Devils! 🔱", "Forks Up!", "Go Devils!",
        "😈 We may be cursed, but at least we're spicy.",
        "🔱 Forking around since 1885.",
        "🤷 Bowl game? We'll take it under advisement.",
        "🔥 We run hot and cold. Mostly cold. But hot sometimes!",
        "😤 Tempe heat isn't the only thing that's brutal around here.",
        "🔱 Small pitchfork energy, big pitchfork dreams.",
        "Bowl eligible is a personality trait at this point.",
        "Forks up. Record down.",
        "Pac-12 refugees still figuring out the Big 12.",
    ],
    "Arkansas": [
        "Woo Pig Sooie! 🐗", "Go Hogs Go!", "Callin' the Hogs!",
        "🐗 We may be hogs, but we're PROUD hogs.",
        "😅 Our defense is a work in progress. A LONG work in progress.",
        "🐷 We're not rebuilding, we're... recalibrating.",
        "🍖 Hog heaven is a state of mind, not a record.",
        "😤 Woo Pig Sooie — said with increasing desperation each week.",
        "Undefeated in tailgating. 2-4 in everything else.",
        "Hogs by mascot. Doormats by scoreboard.",
        "SEC bottom feeder and proud of it.",
    ],
    "Baylor": [
        "Sic 'Em, Bears! 🐻", "That's Baylor... Sic 'Em!",
        "🐻 Bears hibernate. We're just resting up for later.",
        "😬 Sic 'em? More like sit 'em down and hope.",
        "🐻 Slow claps for our slow starts.",
        "😂 We bear-ly made that first down.",
        "🐻 Hibernation season started early this year.",
        "Sic 'Em is more of a suggestion than a strategy.",
        "Bears by mascot. Kittens by 3rd down.",
        "Big 12 attendance numbers carrying harder than the offense.",
    ],
    "California": [
        "Go Bears! 💙💛",
        "🐻 Golden Bears, modest record, unbreakable spirit.",
        "📚 We may not win state, but we ACED it academically.",
        "🌉 Bridging the gap between \"good\" and \"next year.\"",
        "🐻 Cal Bears: nationally ranked in vibes only.",
        "😅 We're not tanking, we're 'strategically evaluating.'",
        "🌉 Golden Gate energy, bronze medal record.",
        "We're 0-4 in conference, but the campus tour was excellent.",
        "Golden Bears. Bronze effort.",
        "ACC transplant still adjusting to actual football weather.",
    ],
    "Colorado": [
        "Go Buffs! 🦬", "CU Boulder, Fight!",
        "🦬 Big, slow, and occasionally dangerous. Just like our offense.",
        "😤 Buffs don't rebuild. We reload. Eventually.",
        "🦬 We stampede... eventually. Once we find our shoes.",
        "😬 Buffs gonna buff (some weeks).",
        "🏔️ Elevation's high. So is our blood pressure watching this team.",
        "We peaked in the preseason hype video.",
        "Buffs stampede. We mosey.",
        "Prime-time hype, undercard results.",
    ],
    "Kentucky": [
        "C-A-T-S! Cats! Cats! Cats! 🐾", "Go Big Blue! 💙",
        "🏀 Ngl we're just here till basketball season.",
        "😹 Wildcats? More like Mildcats. Don't tell Coach.",
        "🐾 We claw our way to 6-6 every year, and we're proud of it.",
        "😂 Big Blue Nation, medium blue expectations.",
        "🏈 Football season: the appetizer before basketball season.",
        "Elite Eight in basketball. Elite excuses in football.",
        "Big Blue Nation. Small blue effort.",
        "We recruit like a football school and play like a JV scrimmage.",
    ],
    "Maryland": [
        "Fear the Turtle! 🐢", "Go Terps! 🐢",
        "🐢 We're not slow, we're deliberate.",
        "😂 Terps by name, turtles by pace.",
        "😅 Big Ten newcomer still figuring out why we're playing Nebraska.",
        "SECU Stadium: plenty of seats, negotiable attendance.",
        "We left the ACC. The ACC did not notice.",
        "🐢 Slow and steady wins the race. We're still working on the winning part.",
        "Fear the Turtle. Mildly acknowledge the record.",
    ],
    "Missouri": [
        "M-I-Z... Z-O-U! 🐯", "Go Tigers!",
        "🐯 Tigers roar. We... politely growl.",
        "🎯 Mizzou: perpetually \"this could be our year.\"",
        "🐯 We're tigers. We just prefer naps to hunting.",
        "😅 M-I-Z... we'll take a win, honestly, any win.",
        "🐅 Roaring on the inside, whimpering on 3rd down.",
        "We're 6-6 in a way that feels intentional at this point.",
        "Tigers on the jersey. Housecats on the field.",
        "SEC middle-of-the-pack, and we've made peace with it.",
    ],
    "Northwestern": [
        "Go 'Cats! 💜", "U Rah Rah!",
        "📚 #1 in the classroom, and that's what matters (please clap).",
        "😬 The 'Cats have claws. Sometimes we even use them.",
        "🎓 We may lose the game, but we WIN the postgame thesis defense.",
        "😹 Wildcats by name, housecats by performance.",
        "💜 Purple hearts, for surviving this season.",
        "We lost the game, but our postgame press conference was Ivy-League caliber.",
        "Smart kids. Dumb red zone decisions.",
        "Big Ten academics carry harder than the Big Ten schedule.",
    ],
    "Oklahoma State": [
        "Go Pokes! 🤠", "Ride 'em, Cowboys!",
        "🤠 We fell off the horse. Getting back on. Eventually.",
        "😂 Cowboys don't cry. We just take a moment.",
        "🤠 We lasso wins. Sometimes the rope's just... too short.",
        "😂 Cowboy up! Or at least cowboy... sideways.",
        "🐴 Giddy up, or at least giddy... eventually.",
        "We're rebuilding. We've been rebuilding since the Clinton administration.",
        "Cowboys by name. Rodeo clowns by 4th quarter.",
        "Big 12 also-ran, and everybody in the league knows it.",
    ],
    "Pittsburgh": [
        "Hail to Pitt! 🐾", "Let's Go, Pitt!",
        "🐆 Panthers are stealthy. Also stealthily bad on 3rd down.",
        "😤 Hail to Pitt, and hail Mary passes.",
        "🐆 Panthers prowl. We mostly just kinda wander.",
        "😅 Hail to Pitt, hail to whatever happens next.",
        "🐾 Building a program, brick by brick. Very small bricks.",
        "We're building a program the same way you build a sandcastle at low tide.",
        "Panthers prowl. We limp.",
        "ACC afterthought, and we've stopped pretending otherwise.",
    ],
    "SMU": [
        "Pony Up! 🐴", "Go Mustangs!",
        "🐴 We may stumble, but we stumble with STYLE.",
        "✨ Mustangs: all flash, occasionally some substance.",
        "🐴 Mustangs run wild. Occasionally in the wrong direction.",
        "😂 We Pony Up, then immediately Pony Down.",
        "✨ Dallas swagger, middling record — we contain multitudes.",
        "Our uniforms have never lost a game. Unfortunately we have to wear them while playing.",
        "All style. Zero substance.",
        "ACC newcomer still figuring out real competition.",
    ],
    "South Carolina": [
        "Go Cocks! 🐔", "Fighting Gamecocks!",
        "🐔 We may not win, but we WILL strut about it.",
        "😂 Fighting Gamecocks, mostly fighting our own mistakes.",
        "🐔 We may lose, but we lose with FEATHERS everywhere.",
        "😂 Cock-a-doodle-don't... but we'll try again next week.",
        "🎶 Sandstorm hype, mid-tier results.",
        "We show up loud and leave quiet. Every single week.",
        "Gamecocks by mascot. Punching bags by scoreboard.",
        "SEC bottom-tier, and Sandstorm can't save us.",
    ],
    "Stanford": [
        "Go Cardinal! 🌲", "Fight, Fight, Fight!",
        "🌲 We're a tree. Trees are patient. VERY patient.",
        "🤓 4.2 GPA, mediocre record, no regrets.",
        "🌲 We photosynthesize wins. Very slowly.",
        "😂 Fight Fight Fight, mostly for a bowl bid.",
        "🤓 We'll out-smart you, if not out-score you.",
        "Our football team has a lower GPA impact than our actual GPA.",
        "Smart school. Dumb defense.",
        "ACC's academic pride, everyone else's easy win.",
    ],
    "Temple": [
        "Fear the Owl! 🦉", "T-U, Owls!",
        "🦉 Fear the Owl! Or at least mildly acknowledge it.",
        "😴 Owls are nocturnal. Our offense only shows up at night too, apparently.",
        "🦉 Whoo's ready for a rebuilding year? We are! Again!",
        "😂 Fear the Owl (mild, occasional fear only).",
        "🦉 We see in the dark. Seeing the scoreboard is harder.",
        "We're the team opposing fans forget was even on the schedule.",
        "Owls by mascot. Pigeons by performance.",
        "American Athletic leftovers, still looking for a signature win.",
    ],
    "Virginia": [
        "Wahoowa! 🔶", "Go Hoos!",
        "⚔️ Cavaliers charge in. We just kind of... amble in.",
        "😬 Wahoowa! Wahoo-ouch.",
        "🔶 Wahoowa, wahoo-uh-oh.",
        "😅 We're Cavaliers. Cavalier about our record too, apparently.",
        "⚔️ We brought a sword to a gunfight, and still tried our best.",
        "We bring school spirit. Everyone else brings a running game.",
        "Cavaliers by name. Pushovers by October.",
        "ACC bottom-half, and the Wahoos know it.",
    ],
    "Virginia Tech": [
        "Let's Go... Hokies! 🦃", "Hokie Pride!",
        "🦃 We may be a turkey, but we're a PROUD turkey.",
        "😅 Enter Sandman hits different when you're down 21.",
        "🦃 Gobble till we wobble, straight to another rebuild.",
        "😂 Hokie Pride, occasionally Hokie Confusion.",
        "🦃 We're not turkeys, we're just misunderstood eagles.",
        "Enter Sandman still hits. Everything after kickoff, less so.",
        "Proud turkey. Regular loser.",
        "Former ACC power, current ACC cautionary tale.",
    ],
    "West Virginia": [
        "Let's Go Mountaineers! 🏔️", "WVU! WVU!",
        "⛰️ We climb mountains. Also our own mistakes, apparently.",
        "😅 Almost heaven? More like almost .500.",
        "⛰️ We climb. Slowly. With frequent breaks.",
        "😅 Let's Go Mountaineers! (Please, someone, let's go.)",
        "🏔️ WVU: peaks and valleys, mostly valleys this year.",
        "We climb mountains. Also our own self-inflicted holes.",
        "Mountaineers by name. Molehills by effort.",
        "Big 12 middle-of-the-road, and we've stopped apologizing for it.",
    ],
    "Wisconsin": [
        "On, Wisconsin! 🦡", "Go Badgers!",
        "🦡 Badgers dig deep. Still digging out of that hole.",
        "🧀 Say cheese! It's the only thing consistently good this year.",
        "🦡 Badgers dig in. Sometimes we dig ourselves a hole instead.",
        "🧀 Cheesehead pride, questionable red zone decisions.",
        "😂 On Wisconsin! (Please, ON. We need the momentum.)",
        "Ground and pound offense. Mostly the ground part.",
        "Cheese is the only thing that's aged well this decade.",
        "Motion-W offense, stagnant everything else.",
    ],
}


def pick_tagline_round_robin(team: str, queue: list, last_used: str = None) -> tuple:
    """
    True round-robin: works through a team's ENTIRE tagline list once
    (in shuffled order) before any of them repeat, then reshuffles for
    the next full cycle. Returns (tagline_or_None, updated_queue) -- the
    caller is responsible for persisting updated_queue to disk (this
    function stays stateless, since check_rta_status.py runs as a fresh
    process every time -- there's no in-memory state that would survive
    between runs).

    team: the team to pick for.
    queue: that team's current remaining-taglines list, as last
        persisted (empty list or None means "start a fresh cycle").
    last_used: the last tagline actually sent for this team, if known --
        used only to avoid the one edge case pure shuffling can't rule
        out on its own: a fresh cycle's first pick happening to be
        identical to the very last thing said before the reshuffle,
        which would still read as a back-to-back repeat.
    """
    options = TEAM_TAGLINES.get(team)
    if not options:
        return None, queue

    if not queue:
        queue = list(options)
        random.shuffle(queue)
        if len(queue) > 1 and queue[0] == last_used:
            queue[0], queue[1] = queue[1], queue[0]

    pick = queue.pop(0)
    return pick, queue


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
    "last_tagline_by_team": {},  # {team: last tagline sent} -- used only for the cycle-boundary check
    "tagline_queue_by_team": {},  # {team: [remaining taglines in the current shuffled cycle]}
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


def process_rta_messages(messages: list, tracked_user_ids: set, id_to_team: dict, state: dict) -> tuple:
    """messages: list of dicts with {"id": str, "author_id": str, "content": str},
    from the MAIN chat channel, oldest-first. Only messages from a
    tracked_user_ids member count -- matched by Discord user ID, not name.

    Returns (state, hits) where hits is a list of {"message_id", "user_id",
    "team"} for every message that counted as a valid RTA this run --
    including repeat declarations from someone already marked ready --
    so the caller can reply to each one with a team-specific chant."""
    ready = set(state.get("ready_user_ids", []))
    last_id = state.get("last_message_id_main")
    hits = []

    for msg in messages:
        if msg["author_id"] in tracked_user_ids and is_rta_message(msg["content"]):
            ready.add(msg["author_id"])
            hits.append({
                "message_id": msg["id"],
                "user_id": msg["author_id"],
                "team": id_to_team.get(msg["author_id"]),
            })
        last_id = msg["id"]

    state["ready_user_ids"] = sorted(ready)
    state["last_message_id_main"] = last_id
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    return state, hits


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


def get_national_champion(season: int, directory: str = ".") -> str | None:
    """
    Reads playoff_bracket_<season>.json (the REAL bracket, never the
    predicted one) and returns the champion's name if the National
    Championship has been decided, else None. This is the authoritative
    check for "has the season actually ended" -- NOT dynasty_data, since
    dynasty_data only contains games for user-controlled teams, and it's
    entirely possible none of them make the National Championship. The
    bracket file always captures the real result regardless of who's in it.
    """
    path = os.path.join(directory, f"playoff_bracket_{season}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    champion = data.get("champion")
    return champion if champion else None


def get_current_season(directory: str = ".") -> int | None:
    """Returns the season number of the newest dynasty_data_<season>.csv
    found, or None if there isn't one yet. Used to know which season's
    playoff bracket file to check for the National Championship result."""
    files = glob.glob(os.path.join(directory, "dynasty_data_*.csv"))
    if not files:
        return None

    def season_num(path):
        m = re.search(r"dynasty_data_(\d+)\.csv$", os.path.basename(path))
        return int(m.group(1)) if m else -1
    latest = max(files, key=season_num)
    n = season_num(latest)
    return n if n != -1 else None


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
    lower = w.lower()
    if "conf" in lower:
        return 900
    bowl_match = re.search(r"bowl\s*(\d+)", lower)
    if bowl_match:
        return 900 + int(bowl_match.group(1))  # Bowl 1 -> 901, Bowl 2 -> 902, etc.
    if "champ" in lower and "conf" not in lower:
        return 950  # National Championship (confirmed label: "Nat'l Champ")
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
