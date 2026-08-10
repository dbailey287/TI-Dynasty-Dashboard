"""
Offline RTA Quip Tester
========================
Tests the actual Gemini connection used for dynamic RTA quips, and shows
you several real generated examples -- without needing Discord, GitHub
Actions, or anything else. Directly reuses rta_logic.py's real functions
(get_team_recent_form, build_quip_prompt, the same model chain), so what
you see here is a genuine preview of what the live bot would produce,
not a separate approximation.

Usage:
    export GENAI_API_KEY=your_real_key
    python test_quips_offline.py Arkansas
    python test_quips_offline.py "South Carolina" --count 8
    python test_quips_offline.py --all-teams
    python test_quips_offline.py --all-teams --count 5 > all_quips.txt
"""
import argparse
import os
import sys

import rta_logic as rl


<<<<<<< HEAD
def generate_examples_for_team(client, team: str, count: int) -> dict:
=======
def generate_examples_for_team(client, gen_config, team: str, count: int) -> dict:
>>>>>>> 55105a57efc302bf254e807273e886a58823161b
    """Prints stats + generated examples for one team. Returns a dict of
    {style_name: count} for that team, or None if there's no data yet."""
    form = rl.get_team_recent_form(team)
    if form is None:
        print(f"⚠️  No completed-game data yet for {team} -- skipping.")
        print()
        return None

    print("=" * 70)
    print(f"{team}")
    print("=" * 70)
    print(f"  Record: {form['wins']}-{form['losses']}  |  "
          f"Last game: {'beat' if form['last_outcome'] == 'W' else 'lost to'} "
          f"{form['last_opponent']} {form['last_team_score']:.0f}-{form['last_opponent_score']:.0f}  |  "
          f"PPG/PA: {form['season_ppg']}/{form['season_pa']}")
    print()

    style_counts = {}
    for i in range(1, count + 1):
        # Built fresh each time -- build_quip_prompt() randomly picks a
        # comedian style internally, same as the real bot does per reply.
        prompt = rl.build_quip_prompt(form)
        style_used = next((s["name"] for s in rl.COMEDIAN_STYLES if s["name"] in prompt), "unknown")

        try:
<<<<<<< HEAD
            response = client.models.generate_content(model=rl.QUIP_MODEL_CHAIN[0], contents=[prompt])
=======
            response = client.models.generate_content(
                model=rl.QUIP_MODEL_CHAIN[0], contents=[prompt], config=gen_config,
            )
>>>>>>> 55105a57efc302bf254e807273e886a58823161b
            text = (response.text or "").strip()
            if text:
                print(f"  [{i}] ({style_used}): {text}")
            else:
                print(f"  [{i}] ({style_used}): -- empty response from the model --")
        except Exception as e:
            print(f"  [{i}] ({style_used}): CONNECTION/API FAILED -- {type(e).__name__}: {e}")

        style_counts[style_used] = style_counts.get(style_used, 0) + 1

    print()
    return style_counts


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("team", nargs="?", default=None,
                         help="Exact team name as it appears in your roster/schedule data. Omit if using --all-teams.")
    parser.add_argument("--count", type=int, default=5, help="How many example quips per team (default 5)")
    parser.add_argument("--all-teams", action="store_true",
                         help="Generate examples for every tracked team instead of just one")
    args = parser.parse_args()

    if not args.all_teams and not args.team:
        print("ERROR: either give a team name, or pass --all-teams.")
        sys.exit(1)

    api_key = os.environ.get("GENAI_API_KEY")
    if not api_key:
        print("ERROR: GENAI_API_KEY must be set to your real key.")
        print("This is the exact same key your GitHub Actions secrets already use.")
        sys.exit(1)

    try:
        from google import genai
    except ImportError:
        print("ERROR: google-genai isn't installed. Run: pip install google-genai")
        sys.exit(1)

    client = genai.Client(api_key=api_key)
<<<<<<< HEAD
=======
    # Same temperature as the real production path in rta_logic.py's
    # generate_dynamic_quip() -- keeping this in sync matters, otherwise
    # this tester would give unrepresentatively repetitive results
    # compared to what the live bot actually produces.
    from google.genai import types
    gen_config = types.GenerateContentConfig(temperature=1.3)
>>>>>>> 55105a57efc302bf254e807273e886a58823161b

    if args.all_teams:
        teams = sorted(rl.TEAM_TAGLINES.keys())
        print(f"Generating {args.count} example(s) each for {len(teams)} team(s)...")
        print("(This makes real API calls for every team -- may take a little while.)")
        print()
        overall_style_counts = {}
        skipped = []
        for team in teams:
<<<<<<< HEAD
            result = generate_examples_for_team(client, team, args.count)
=======
            result = generate_examples_for_team(client, gen_config, team, args.count)
>>>>>>> 55105a57efc302bf254e807273e886a58823161b
            if result is None:
                skipped.append(team)
            else:
                for style, n in result.items():
                    overall_style_counts[style] = overall_style_counts.get(style, 0) + n

        print("=" * 70)
        print("SUMMARY")
        print("=" * 70)
        print("Overall style distribution:", overall_style_counts)
        if skipped:
            print(f"Skipped (no completed-game data yet): {', '.join(skipped)}")
    else:
        form_check = rl.get_team_recent_form(args.team)
        if form_check is None:
            print(f"No completed-game data found for '{args.team}' -- can't build a quip yet.")
            print("(Check the team name matches exactly, and that dynasty_data_<season>.csv")
            print(" is in this same folder.)")
            sys.exit(1)
<<<<<<< HEAD
        style_counts = generate_examples_for_team(client, args.team, args.count)
=======
        style_counts = generate_examples_for_team(client, gen_config, args.team, args.count)
>>>>>>> 55105a57efc302bf254e807273e886a58823161b
        print("=" * 70)
        print("Style distribution this run:", style_counts)
        print("(Random per call -- run again to see a different mix.)")
        print("=" * 70)


if __name__ == "__main__":
    main()
