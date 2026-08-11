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
import logging
import os
import random
import re
from datetime import datetime, timezone

import roster as _roster

log = logging.getLogger("rta_logic")

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

# OPTIONAL per-team override for real program history. By default,
# build_quip_prompt() lets Gemini draw on its own general knowledge of a
# team's football history/reputation -- accuracy isn't required to be
# perfect there. If a team has an entry HERE, that specific fact is used
# instead of letting the model free-associate -- useful as a correction
# lever if a generated line ever gets a detail wrong enough to be worth
# pinning down, or for a fact worth locking in exactly as-is. Entries
# here are individually verified against a real source before adding
# (same standard as confirming "Fear the Turtle" was real). Most teams
# won't have an entry, and that's fine -- it's an override, not a gate.
TEAM_HISTORY_FACTS = {
    # No overrides currently in use -- every team goes through the
    # general-knowledge Gemini path in build_quip_prompt() uniformly.
    # Add a team here later if a generated line ever needs a specific
    # fact pinned down/corrected.
}


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


# ---------------------------------------------------------------------------
# Data-driven quips (week 3+)
# ---------------------------------------------------------------------------
# Weeks 0-2, there's rarely enough real season data to say anything
# specific yet, so RTA replies use the static TEAM_TAGLINES bank above.
# From week 3 on, replies instead reference this team's ACTUAL season so
# far -- record, last game, streak -- generated fresh per reply via
# Gemini rather than picked from a fixed list, so every reply can be
# unique. Falls back to the static bank on any failure (missing data,
# missing API key, API error) -- this always has to degrade gracefully,
# since a broken quip generator should never mean no reply at all.

DYNAMIC_QUIP_MIN_WEEK_SORT = 3
QUIP_MODEL_CHAIN = ["gemini-flash-lite-latest", "gemini-3.5-flash-lite", "gemini-flash-latest"]
QUIP_RETRIES_PER_MODEL = 2


def get_team_recent_form(team: str, directory: str = ".", current_week_sort: int = None) -> dict | None:
    """
    Pulls this team's actual season-so-far from the current season's
    dynasty_data_<season>.csv: record, current streak, last completed
    game, and season PPG/PA. Returns None if there's no completed-game
    data yet for this team (too early in the season, or the file's
    missing/unreadable) -- callers should fall back to the static
    tagline bank in that case, not treat it as an error.

    Deliberately only looks at COMPLETED games for record/streak/last-game
    facts. At the moment someone posts RTA to advance past the current
    week, that week's own game(s) aren't done yet in real time -- but the
    CSV's Status field can lag even further behind: it only updates when
    the daily schedule scraper next runs and picks up a NEW screenshot,
    which could be hours or a full day after the game was actually
    played. So the current week's game can still show "Upcoming" in the
    data well after someone has already played it and posted RTA.

    current_week_sort (from rta_status.json, the same tracked value used
    elsewhere for "what week are we on") is used specifically to keep
    upcoming-opponent facts genuinely in the future -- any week at or
    before current_week_sort is excluded from next_opponent/
    next_ranked_opponent/next_user_opponent, regardless of what its
    Status column says, since a stale "Upcoming" status for a game
    that's already been played would otherwise get referenced as if it
    hasn't happened yet. If current_week_sort isn't passed, falls back
    to trusting Status alone (only used by the standalone offline tester
    when rta_status.json isn't available to it).
    """
    df = _load_latest_season_df(directory)
    if df is None:
        return None
    import pandas as pd

    team_games = df[(df["Team"] == team) & (df["Status"] == "Completed")].copy()
    if team_games.empty:
        return None

    team_games["_sort"] = team_games["Week"].apply(week_sort_key)
    team_games = team_games.sort_values("_sort")

    wins = int((team_games["Outcome"] == "W").sum())
    losses = int((team_games["Outcome"] == "L").sum())

    # Current streak: walk backward from the most recent game while the
    # outcome keeps matching.
    outcomes = list(team_games["Outcome"])
    streak_type = outcomes[-1]
    streak_len = 0
    for o in reversed(outcomes):
        if o == streak_type:
            streak_len += 1
        else:
            break

    last = team_games.iloc[-1]
    try:
        team_score = float(last["Team_Score"])
        opp_score = float(last["Opponent_Score"])
    except (ValueError, TypeError):
        team_score = opp_score = None

    scores = team_games[["Team_Score", "Opponent_Score"]].apply(pd.to_numeric, errors="coerce")
    ppg = scores["Team_Score"].mean()
    pa = scores["Opponent_Score"].mean()

    # Record vs ranked opponents, and vs fellow user-controlled teams --
    # separate from the overall record, since these are more specific,
    # more interesting joke material than just the plain W-L.
    ranked_games = team_games[team_games["Opponent_Rank"].notna() & (team_games["Opponent_Rank"] != "-")]
    ranked_wins = int((ranked_games["Outcome"] == "W").sum())
    ranked_losses = int((ranked_games["Outcome"] == "L").sum())

    user_games = team_games[team_games["Matchup_Type"] == "User vs User"]
    user_wins = int((user_games["Outcome"] == "W").sum())
    user_losses = int((user_games["Outcome"] == "L").sum())

    # Upcoming opponents -- real facts about what's actually on the
    # schedule, not a prediction of how those games will go. The
    # immediate next game, the next RANKED opponent, and the next
    # fellow-USER opponent are tracked separately since they're often
    # different weeks (e.g. next game is an easy unranked team, but a
    # ranked or user matchup looms a few weeks out).
    #
    # Reference point for "what counts as genuinely upcoming": prefer
    # the authoritative current_week_sort if the caller passed one in,
    # since the CSV's own Status column can still say "Upcoming" for a
    # game that's already been played but hasn't been scraped yet --
    # without this, that just-played game could get referenced as a
    # future opponent. Falls back to the last completed game's week if
    # no current_week_sort was given (standalone/offline testing only).
    reference_week_sort = current_week_sort if current_week_sort is not None else team_games["_sort"].iloc[-1]

    upcoming = df[(df["Team"] == team) & (df["Status"] == "Upcoming")].copy()
    next_opponent = next_opponent_rank = next_opponent_is_user = None
    next_ranked_opponent = next_ranked_opponent_rank = next_ranked_weeks_away = None
    next_user_opponent = next_user_weeks_away = None
    if not upcoming.empty:
        upcoming["_sort"] = upcoming["Week"].apply(week_sort_key)
        upcoming = upcoming[upcoming["_sort"] > reference_week_sort]
        upcoming = upcoming.sort_values("_sort")

    if not upcoming.empty:
        first = upcoming.iloc[0]
        next_opponent = first["Opponent"]
        next_opponent_rank = None if first["Opponent_Rank"] in (None, "-") or pd.isna(first["Opponent_Rank"]) else first["Opponent_Rank"]
        next_opponent_is_user = first["Matchup_Type"] == "User vs User"

        ranked_upcoming = upcoming[upcoming["Opponent_Rank"].notna() & (upcoming["Opponent_Rank"] != "-")]
        if not ranked_upcoming.empty:
            r = ranked_upcoming.iloc[0]
            next_ranked_opponent = r["Opponent"]
            next_ranked_opponent_rank = r["Opponent_Rank"]
            next_ranked_weeks_away = int(r["_sort"] - reference_week_sort)

        user_upcoming = upcoming[upcoming["Matchup_Type"] == "User vs User"]
        if not user_upcoming.empty:
            u = user_upcoming.iloc[0]
            next_user_opponent = u["Opponent"]
            next_user_weeks_away = int(u["_sort"] - reference_week_sort)

    return {
        "team": team,
        "wins": wins,
        "losses": losses,
        "streak_type": streak_type,       # "W" or "L"
        "streak_len": streak_len,
        "last_opponent": last["Opponent"],
        "last_outcome": last["Outcome"],
        "last_team_score": team_score,
        "last_opponent_score": opp_score,
        "season_ppg": None if pd.isna(ppg) else round(float(ppg), 1),
        "season_pa": None if pd.isna(pa) else round(float(pa), 1),
        "ranked_wins": ranked_wins,
        "ranked_losses": ranked_losses,
        "user_wins": user_wins,
        "user_losses": user_losses,
        "next_opponent": next_opponent,
        "next_opponent_rank": next_opponent_rank,
        "next_opponent_is_user": next_opponent_is_user,
        "next_ranked_opponent": next_ranked_opponent,
        "next_ranked_opponent_rank": next_ranked_opponent_rank,
        "next_ranked_weeks_away": next_ranked_weeks_away,
        "next_user_opponent": next_user_opponent,
        "next_user_weeks_away": next_user_weeks_away,
    }


# Comedian voice styles for RTA quips -- one is randomly chosen per quip
# in build_quip_prompt() below. Add more anytime: each entry just needs
# a "name" and a "style" description to inject into the prompt. Style
# descriptions describe WRITING voice -- word choice, pacing, level of
# detail, restraint vs escalation, how mean vs gentle -- since actual
# vocal delivery/timing doesn't translate into a single written line
# regardless of how the prompt is worded; that's a real limit of text,
# not something to try to prompt around.
# Hard-enforced at the code level in generate_dynamic_quip() below, not
# just requested in the prompt -- real production output showed the
# banned-openers prompt instruction being ignored a large fraction of
# the time (a soft "don't do X" request the model can simply not
# follow), and profanity slipping through with no rule against it at
# all. A deterministic check-and-retry is the only reliable way to
# actually guarantee these hold, rather than hoping wording alone works.
BANNED_OPENERS = (
    "ready to advance", "well, you're", "well you're", "well,",
    "rta?!", "rta ", "rta,", "man, ", "man,",
)
# Not exhaustive profanity detection (that's a much bigger problem than
# this needs to solve) -- just the small set of words that actually
# showed up in real output, checked as a substring on the lowercased text.
PROFANITY_MARKERS = ("fuck", "shit", "bitch", "asshole", "cunt", "damn it", "goddamn")


def _quip_violates_rules(text: str) -> str | None:
    """Returns a short reason string if the generated text breaks a hard
    rule, else None. Used to trigger a retry rather than just log and
    accept a bad line."""
    lower = text.strip().lower()
    for opener in BANNED_OPENERS:
        if lower.startswith(opener):
            return f"starts with banned opener '{opener}'"
    for word in PROFANITY_MARKERS:
        if word in lower:
            return f"contains profanity marker '{word}'"
    return None


COMEDIAN_STYLES = [
    {
        "name": "Nate Bargatze",
        "examples": [
            "We're 6-0, and I keep waiting for someone to tell me it doesn't count, because deep down I still don't fully believe it either.",
            "Giving up 30 a game and still winning just means we'd rather have a heart attack than win by two scores like normal people do.",
            "I don't know how we're 5-1. Nobody in that building has an explanation for it. We're just quietly hoping nobody looks too closely at the tape.",
            "We won by 3 points and everybody's celebrating like we won a championship, when really we just barely avoided total humiliation by one bad snap.",
        ],
    },
    {
        "name": "Derrick Stroup",
        "examples": [
            "6-0 and you're out here talking like you built a dynasty, but you're scoring 40 and giving up 35, my brother, that is a coin flip with extra steps!",
            "You win by 3 points and you're strutting around like you're Nick Saban?! 3 points is barely a football game, that is a rounding error with a scoreboard attached to it!",
            "Giving up 30 a game and STILL smiling?! I have seen hostages negotiate better terms than whatever your defense is out there agreeing to every single Saturday!",
            "6-0 is cute, sure, but you play the #1 team next week, and I promise you they have eaten better offenses than yours for a pregame snack!",
        ],
    },
]


def build_quip_prompt(form: dict) -> str:
    """Builds the Gemini prompt for a single dynamic RTA-reply quip,
    using only the real facts in `form` -- never inventing anything the
    data doesn't actually say. Randomly picks one comedian voice from
    COMEDIAN_STYLES per call, so replies vary between styles over time.

    Deliberately lean: earlier versions stacked many simultaneous
    requirements (mandatory joke structure, minimum fact count, number
    formatting, several banned-phrase rules restated more than once) on
    top of the actual creative ask, and real output got flatter and more
    repetitive the more rules got added -- comedy loses that fight
    against constraint-satisfaction. This version leads with concrete
    example lines to carry the voice (a much stronger signal than prose
    description) and keeps the hard rules to a short list stated once."""
    team = form["team"]
    record = f"{form['wins']}-{form['losses']}"
    streak_word = "winning" if form["streak_type"] == "W" else "losing"
    streak = f"a {form['streak_len']}-game {streak_word} streak" if form["streak_len"] > 1 else None
    last_result = (
        f"{'beat' if form['last_outcome'] == 'W' else 'lost to'} {form['last_opponent']} "
        f"{form['last_team_score']:.0f}-{form['last_opponent_score']:.0f}"
        if form["last_team_score"] is not None else None
    )

    facts = [f"Record: {record}."]
    if last_result:
        facts.append(f"Most recent game: {last_result}.")
    if streak:
        facts.append(f"Currently on {streak}.")
    if form["season_ppg"] is not None and form["season_pa"] is not None:
        facts.append(f"Averaging {form['season_ppg']} points scored, {form['season_pa']} allowed per game.")
    if form["ranked_wins"] + form["ranked_losses"] > 0:
        facts.append(f"Record vs ranked opponents this season: {form['ranked_wins']}-{form['ranked_losses']}.")
    if form["user_wins"] + form["user_losses"] > 0:
        facts.append(f"Record vs fellow user-controlled teams this season: {form['user_wins']}-{form['user_losses']}.")
    same_upcoming_game = (
        form.get("next_ranked_opponent") and form.get("next_user_opponent")
        and form["next_ranked_opponent"] == form["next_user_opponent"]
        and form["next_ranked_weeks_away"] == form["next_user_weeks_away"]
    )
    if same_upcoming_game:
        weeks = form["next_ranked_weeks_away"]
        when = "next week" if weeks == 1 else f"in {weeks} weeks"
        facts.append(
            f"Upcoming: plays #{form['next_ranked_opponent_rank']} {form['next_ranked_opponent']} "
            f"{when} -- both a ranked opponent AND a fellow user-controlled team."
        )
    else:
        if form.get("next_ranked_opponent"):
            weeks = form["next_ranked_weeks_away"]
            when = "next week" if weeks == 1 else f"in {weeks} weeks"
            facts.append(f"Upcoming: plays #{form['next_ranked_opponent_rank']} {form['next_ranked_opponent']} {when}.")
        if form.get("next_user_opponent"):
            weeks = form["next_user_weeks_away"]
            when = "next week" if weeks == 1 else f"in {weeks} weeks"
            facts.append(f"Upcoming: plays fellow user-controlled team {form['next_user_opponent']} {when}.")
    facts_block = " ".join(facts)

    history_options = TEAM_HISTORY_FACTS.get(team, [])
    if history_options:
        history_fact = random.choice(history_options)
        history_block = f"\nOne real historical fact you could optionally use (not from this season): {history_fact}"
    else:
        history_block = (
            f"\nYou can also optionally draw on real general knowledge of {team}'s "
            f"football program/reputation if it strengthens the joke -- approximate "
            f"details are fine, just keep it genuinely recognizable, not invented."
        )

    chosen_style = random.choice(COMEDIAN_STYLES)
    examples_block = "\n".join(f'- "{ex}"' for ex in chosen_style["examples"])

    return f"""Write ONE funny heckle line (25-40 words -- give yourself real room for a
setup and a turn, not just a one-beat jab) for a college football group
chat, roasting {team} using their real stats below. Someone just
posted "RTA" for this team -- everyone already knows that, don't mention
it or reference advancing at all, just roast their season.

Real facts about {team} -- use only these, don't invent anything else:
{facts_block}{history_block}

Pick whichever ONE or TWO of these facts actually make for the funniest
line -- you don't need to use all of them, and don't just list them with
"while"/"after"/"since". This needs a real punchline: a genuine turn, an
unexpected comparison, an ironic angle -- something that actually reads
as a joke, not a stats recap with attitude. Go for real trash talk --
sharp and a little mean, not a gentle ribbing. Don't hedge the insult.

The ENDING is the most important part -- that's where the joke actually
has to land. Don't trail off into something vague like "...which should
make watching them next week real comfortable for everybody" or
"...which should be real fun when they show up" -- those are gestures
at a joke, not an actual joke. Commit to something SPECIFIC and vivid
at the end: a real image, a concrete comparison, an actual punchline --
not a vague wave at future doom.

If it's in the facts above, one especially strong angle is pairing a
PAST result with a FUTURE one -- e.g. their record against ranked teams
plus an upcoming ranked opponent, or their record against fellow users
plus an upcoming user matchup -- building dread or false confidence
about what's coming. Only use this if those specific facts are actually
listed above, and don't force it if a simpler single-fact joke lands
better. If you use this angle, the future part still needs a SPECIFIC
ending, not a vague one -- see the rule above.

Write it in this voice -- study these examples closely, they're the
actual target sound, not just a topic area. Notice they don't all end
the same way -- vary your own sentence structure and ending too, don't
fall back on one template:
{examples_block}

(Those examples are about hypothetical teams -- write a NEW line for
{team} specifically, don't reuse their wording, scenario, or the exact
way any of them end.)

Hard rules: numbers as numerals ("4-4" and "47-45", never spelled out).
No profanity. No mention of RTA/advancing. You CAN reference an upcoming
opponent as a real schedule fact (who they play, when) if it's listed
above, but never predict or claim to know how that game actually turns
out -- nobody knows that yet. At most one emoji. Return ONLY the line
itself, nothing else.
"""


def generate_dynamic_quip(form: dict, api_key: str) -> str | None:
    """Synchronous Gemini call (this script has no async event loop, so
    no need to wrap it) generating one fresh trash-talk line from a
    team's real season data. Returns None on any failure -- missing key,
    API error, empty response -- so the caller can fall back to the
    static tagline bank rather than skip the reply entirely."""
    if not api_key:
        log.warning("generate_dynamic_quip: no API key provided (GENAI_API_KEY not set).")
        return None
    try:
        from google import genai
        from google.genai.errors import APIError
    except ImportError as e:
        log.error("generate_dynamic_quip: google-genai package not installed (%s) -- check the workflow's pip install step.", e)
        return None

    client = genai.Client(api_key=api_key)
    # Explicit higher temperature -- previously unset (whatever the
    # model's own default is), which real output showed converging hard:
    # some generations for the same team came back character-for-character
    # identical to each other. 1.3 pushes toward meaningfully more varied
    # phrasing while staying coherent; QUIP_MODEL_CHAIN's flash-lite
    # models generally tolerate this range fine for short creative text.
    gen_config = genai.types.GenerateContentConfig(temperature=1.3)

    last_error = None
    for model_name in QUIP_MODEL_CHAIN:
        for attempt in range(1, QUIP_RETRIES_PER_MODEL + 1):
            # Fresh prompt each attempt -- a new random style/angle, so a
            # retry actually has a real chance of avoiding whatever
            # pattern triggered the last attempt, rather than sending the
            # identical prompt again and hoping temperature alone saves it.
            prompt = build_quip_prompt(form)
            try:
                response = client.models.generate_content(model=model_name, contents=[prompt], config=gen_config)
                text = (response.text or "").strip()
                if not text:
                    log.warning("generate_dynamic_quip: %s (attempt %d) returned an empty response.", model_name, attempt)
                    continue
                violation = _quip_violates_rules(text)
                if violation:
                    log.warning("generate_dynamic_quip: %s (attempt %d) rejected -- %s. Text was: %r", model_name, attempt, violation, text)
                    continue
                return text
            except APIError as e:
                last_error = e
                log.warning("generate_dynamic_quip: %s (attempt %d) failed: %s", model_name, attempt, e)
    log.error("generate_dynamic_quip: all models/retries exhausted. Last error: %s", last_error)
    return None


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
