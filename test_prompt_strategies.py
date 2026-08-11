"""
Prompt Strategy Tester
=======================
Generates several structurally DIFFERENT candidate prompts for RTA
heckle lines, so you can copy-paste each one into Gemini's own web UI
and compare real results side by side -- no API key, no API calls, no
cost. This is purely a prompt printer.

Sixteen strategies, each testing a different axis of "what actually makes
these funny":
  history           - comedy about the school's football history/reputation,
                       accuracy not required
  vs_ranked_user     - comedy specifically about record vs ranked/user teams
  advance_generic    - a generic joke about wanting to advance, not team-specific
  advance_team       - same, but flavored for a specific team's identity
  no_impersonation   - real facts, but no "sound like X comedian" instruction
  single_fact        - one randomly chosen fact from a SMALL pool (record/last
                        game/PPG/streak only), lots of creative room
  character          - a fictional persona (rival coach) instead of a real comedian
  roast_genre        - explicitly framed as a Comedy Central Roast-style joke
  absurdist          - almost no real facts, just "be weird and funny"
  random_1_fact      - one randomly chosen fact from the FULL pool (also
                        includes ranked/user record and upcoming opponents)
  random_2_facts     - two randomly chosen facts, look for a connection between them
  random_subset      - a random COUNT (1-3) of random facts each run
  school_vibe        - the program/fanbase's general stereotype or culture
  school_rivalry     - their biggest rivalry, not tied to this season's stats
  school_mascot      - a bit built around the mascot as a character
  school_reputation  - a short, punchy line about their overall reputation

Usage:
    python test_prompt_strategies.py Arkansas
    python test_prompt_strategies.py Arkansas --strategy history
    python test_prompt_strategies.py Arkansas --strategy random_1_fact school_vibe
    python test_prompt_strategies.py Arkansas > prompts_arkansas.txt
"""
import argparse
import random
import sys

import rta_logic as rl

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def _facts_block(form: dict) -> str:
    """Same fact-building logic as build_quip_prompt, kept separate here
    so strategies that want the full bundle can reuse it without pulling
    in the rest of that function's prompt-specific wording."""
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
        facts.append(f"Record vs ranked opponents: {form['ranked_wins']}-{form['ranked_losses']}.")
    if form["user_wins"] + form["user_losses"] > 0:
        facts.append(f"Record vs fellow user-controlled teams: {form['user_wins']}-{form['user_losses']}.")
    return " ".join(facts)


def _fact_pool(form: dict) -> list:
    """Same facts as _facts_block, but as a list of individual strings
    instead of one joined block -- lets the randomized strategies below
    sample a genuinely random SUBSET rather than always getting the same
    fixed bundle. Includes upcoming-opponent facts too, which the fixed
    bundle above doesn't."""
    pool = [f"Record: {form['wins']}-{form['losses']}."]
    if form["last_team_score"] is not None:
        outcome = "beat" if form["last_outcome"] == "W" else "lost to"
        pool.append(f"Most recent game: {outcome} {form['last_opponent']} {form['last_team_score']:.0f}-{form['last_opponent_score']:.0f}.")
    if form["streak_len"] > 1:
        streak_word = "winning" if form["streak_type"] == "W" else "losing"
        pool.append(f"Currently on a {form['streak_len']}-game {streak_word} streak.")
    if form["season_ppg"] is not None and form["season_pa"] is not None:
        pool.append(f"Averaging {form['season_ppg']} points scored, {form['season_pa']} allowed per game.")
    if form["ranked_wins"] + form["ranked_losses"] > 0:
        pool.append(f"Record vs ranked opponents: {form['ranked_wins']}-{form['ranked_losses']}.")
    if form["user_wins"] + form["user_losses"] > 0:
        pool.append(f"Record vs fellow user-controlled teams: {form['user_wins']}-{form['user_losses']}.")
    if form.get("next_ranked_opponent"):
        weeks = form["next_ranked_weeks_away"]
        when = "next week" if weeks == 1 else f"in {weeks} weeks"
        pool.append(f"Upcoming: plays #{form['next_ranked_opponent_rank']} {form['next_ranked_opponent']} {when}.")
    if form.get("next_user_opponent"):
        weeks = form["next_user_weeks_away"]
        when = "next week" if weeks == 1 else f"in {weeks} weeks"
        pool.append(f"Upcoming: plays fellow user-controlled team {form['next_user_opponent']} {when}.")
    return pool


# ---------------------------------------------------------------------------
# The 9 strategies. Each takes (team, form) -- form may be None for
# strategies that don't need real stats -- and returns prompt text.
# ---------------------------------------------------------------------------

def strategy_history(team, form):
    return f"""Write ONE funny line (30-50 words) for a college football group chat,
joking about {team}'s football program -- its history, reputation, a
famous era, a well-known coach, a rivalry, a notable good or bad season,
anything genuinely recognizable about this specific program.

Exact accuracy isn't required -- approximate details are fine, this is
about the joke landing, not a fact-check. Just make sure it's clearly
about {team} specifically, not something generic that could apply to
any team. No profanity. At most one emoji. Return ONLY the line itself,
no quotes, no preamble.
"""


def strategy_vs_ranked_user(team, form):
    ranked = f"{form['ranked_wins']}-{form['ranked_losses']}" if form and (form['ranked_wins'] + form['ranked_losses'] > 0) else "no games yet"
    user = f"{form['user_wins']}-{form['user_losses']}" if form and (form['user_wins'] + form['user_losses'] > 0) else "no games yet"
    overall = f"{form['wins']}-{form['losses']}" if form else "unknown"
    return f"""Real facts about {team} this season: overall record {overall}. Record
against ranked opponents: {ranked}. Record against fellow user-controlled
teams: {user}.

Write ONE funny, sharp line (30-50 words) roasting {team} specifically
using how they've done against ranked opponents and/or fellow
user-controlled teams this season -- that's the actual subject, not
their overall record. Real trash talk, not gentle ribbing. No profanity.
At most one emoji. Return ONLY the line itself, no quotes, no preamble.
"""


def strategy_advance_generic(team, form):
    return """Write ONE funny, short joke (20-40 words) about the general experience
of playing a college football dynasty/franchise-mode league with
friends and everyone waiting on each other to be ready to advance to
the next week -- the anxiety of waiting, the arms race of stats, the
group-chat energy of it. NOT about any specific team.

No profanity. At most one emoji. Return ONLY the line itself, no
quotes, no preamble.
"""


def strategy_advance_team(team, form):
    return f"""Write ONE funny joke (20-40 words) about being the coach of {team} in a
college football dynasty league, specifically about the experience of
being ready to advance to the next week. Can reference {team}'s general
vibe, mascot, or identity -- doesn't need real season stats.

No profanity. At most one emoji. Return ONLY the line itself, no
quotes, no preamble.
"""


def strategy_no_impersonation(team, form):
    facts = _facts_block(form) if form else f"(no completed-game data yet for {team})"
    return f"""Real facts about {team} this season -- use only these, don't invent
anything else: {facts}

Write ONE sharp, funny heckle line (30-50 words) roasting {team} using
these real facts. Just be genuinely funny and a little mean -- don't
try to sound like any particular comedian or persona, just write the
sharpest, funniest line you can on your own terms.

No profanity. At most one emoji. Return ONLY the line itself, no
quotes, no preamble.
"""


def strategy_single_fact(team, form):
    if not form:
        fact = f"(no completed-game data yet for {team})"
    else:
        candidates = [f"Record: {form['wins']}-{form['losses']}."]
        if form["last_team_score"] is not None:
            outcome = "beat" if form["last_outcome"] == "W" else "lost to"
            candidates.append(f"Most recent game: {outcome} {form['last_opponent']} {form['last_team_score']:.0f}-{form['last_opponent_score']:.0f}.")
        if form["season_ppg"] is not None:
            candidates.append(f"Averaging {form['season_ppg']} points scored, {form['season_pa']} allowed per game.")
        if form["streak_len"] > 1:
            streak_word = "winning" if form["streak_type"] == "W" else "losing"
            candidates.append(f"Currently on a {form['streak_len']}-game {streak_word} streak.")
        fact = random.choice(candidates)

    return f"""One real fact about {team} this season: {fact}

Write ONE funny, creative heckle line (30-50 words) built from just this
single fact -- you have a lot of room here, so go somewhere unexpected
with it rather than just restating it. Don't invent any other facts,
but do whatever you want creatively with this one.

No profanity. At most one emoji. Return ONLY the line itself, no
quotes, no preamble.
"""


def strategy_character(team, form):
    facts = _facts_block(form) if form else f"(no completed-game data yet for {team})"
    return f"""You are playing a character: a rival coach in this dynasty league who
takes the whole thing way too personally and is a little unhinged about
it -- not a real comedian, just this specific fictional persona.

Real facts about {team} this season -- use only these: {facts}

In that character's voice, write ONE funny heckle line (30-50 words)
roasting {team} using these facts.

No profanity. At most one emoji. Return ONLY the line itself, no
quotes, no preamble.
"""


def strategy_roast_genre(team, form):
    facts = _facts_block(form) if form else f"(no completed-game data yet for {team})"
    return f"""Real facts about {team} this season -- use only these: {facts}

Write ONE line (30-50 words) in the style of a Comedy Central Roast
joke -- sharp, a little savage, structured like a real roast joke (a
setup that lands on a real insult, not just an observation) -- about
{team}, using these real facts.

No profanity. At most one emoji. Return ONLY the line itself, no
quotes, no preamble.
"""


def strategy_absurdist(team, form):
    return f"""Write ONE absurd, weird, genuinely funny line (20-40 words) about
{team}'s football team. Doesn't need to be based on real stats or facts
at all -- just needs to be surreal and funny. Go somewhere unexpected.

No profanity. At most one emoji. Return ONLY the line itself, no
quotes, no preamble.
"""


# ---------------------------------------------------------------------------
# Randomized-stats variants. Testing results suggested the sharpest lines
# used just 1-2 facts, never the full bundle -- these three test slightly
# different ways of narrowing that down, since WHICH facts show up (and
# how many) genuinely varies each run.
# ---------------------------------------------------------------------------

def strategy_random_1_fact(team, form):
    pool = _fact_pool(form) if form else [f"(no completed-game data yet for {team})"]
    fact = random.choice(pool)
    return f"""One real fact about {team} this season: {fact}

Write ONE funny, sharp roast line (25-45 words) built from just this
single fact -- you have a lot of room here, go somewhere unexpected
with it rather than just restating it. Real trash talk, sharp and a
little mean.

No profanity. At most one emoji. Return ONLY the line itself, no
quotes, no preamble.
"""


def strategy_random_2_facts(team, form):
    pool = _fact_pool(form) if form else [f"(no completed-game data yet for {team})"]
    chosen = random.sample(pool, k=min(2, len(pool)))
    facts = " ".join(chosen)
    return f"""Real facts about {team} this season: {facts}

Write ONE funny, sharp roast line (25-45 words) using these two facts
-- find the connection or contrast between them if there is one, don't
just state them back to back. Real trash talk, sharp and a little mean.

No profanity. At most one emoji. Return ONLY the line itself, no
quotes, no preamble.
"""


def strategy_random_subset(team, form):
    pool = _fact_pool(form) if form else [f"(no completed-game data yet for {team})"]
    k = random.randint(1, min(3, len(pool)))
    chosen = random.sample(pool, k=k)
    facts = " ".join(chosen)
    return f"""Real facts about {team} this season: {facts}

Write ONE funny, sharp roast line (25-45 words) using whichever of these
facts actually makes for the funniest line -- you don't need to use all
of them if fewer works better. Real trash talk, sharp and a little mean.

No profanity. At most one emoji. Return ONLY the line itself, no
quotes, no preamble.
"""


# ---------------------------------------------------------------------------
# Generic school-info variants. Different subject angles for "about the
# school, but not stat-bound" -- vibe/culture, rivalry, mascot, and a
# short reputation-based one-liner, rather than all converging on
# "history" the way the original history strategy does.
# ---------------------------------------------------------------------------

def strategy_school_vibe(team, form):
    return f"""Write ONE funny line (25-45 words) about {team}'s general vibe as a
football program and fanbase -- the stereotype, the culture, the kind of
program it's known as (not necessarily historical facts, just the
overall "personality" of the school in college football).

Approximate/stereotypical is fine here -- this is about the joke, not
precision. No profanity. At most one emoji. Return ONLY the line itself,
no quotes, no preamble.
"""


def strategy_school_rivalry(team, form):
    return f"""Write ONE funny line (25-45 words) about {team}'s biggest rivalry in
college football -- who they hate playing, or who historically gives
them trouble, or the general dynamic of that rivalry. Approximate
details are fine, doesn't need to be perfectly accurate.

No profanity. At most one emoji. Return ONLY the line itself, no
quotes, no preamble.
"""


def strategy_school_mascot(team, form):
    return f"""Write ONE funny, absurd line (25-45 words) built around {team}'s
mascot specifically -- treat the mascot almost like a character with its
own personality, and build a joke out of that.

No profanity. At most one emoji. Return ONLY the line itself, no
quotes, no preamble.
"""


def strategy_school_reputation(team, form):
    return f"""Write ONE short, punchy line (15-25 words -- keep this one tight, not
long) about {team}'s overall reputation in college football -- are they
a blue blood, a perennial underachiever, a scrappy nobody, a program
everyone overrates, etc. Whatever's genuinely recognizable about them.

No profanity. At most one emoji. Return ONLY the line itself, no
quotes, no preamble.
"""


STRATEGIES = {
    "history": strategy_history,
    "vs_ranked_user": strategy_vs_ranked_user,
    "advance_generic": strategy_advance_generic,
    "advance_team": strategy_advance_team,
    "no_impersonation": strategy_no_impersonation,
    "single_fact": strategy_single_fact,
    "character": strategy_character,
    "roast_genre": strategy_roast_genre,
    "absurdist": strategy_absurdist,
    "random_1_fact": strategy_random_1_fact,
    "random_2_facts": strategy_random_2_facts,
    "random_subset": strategy_random_subset,
    "school_vibe": strategy_school_vibe,
    "school_rivalry": strategy_school_rivalry,
    "school_mascot": strategy_school_mascot,
    "school_reputation": strategy_school_reputation,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("team", help="Exact team name as it appears in your roster/schedule data")
    parser.add_argument(
        "--strategy", nargs="+", choices=list(STRATEGIES.keys()), default=None,
        help="Which strategy/strategies to print. Omit to print all 9.",
    )
    args = parser.parse_args()

    form = rl.get_team_recent_form(args.team)
    if form is None:
        print(f"NOTE: no completed-game data yet for {args.team} -- strategies that")
        print("need real stats will note that instead of using facts. history/")
        print("advance_generic/advance_team/absurdist don't need real data anyway.")
        print()

    strategies_to_run = args.strategy or list(STRATEGIES.keys())

    for name in strategies_to_run:
        print("=" * 70)
        print(f"STRATEGY: {name}")
        print("=" * 70)
        print(STRATEGIES[name](args.team, form))
        print()


if __name__ == "__main__":
    main()
