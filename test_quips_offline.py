"""
Offline RTA Quip Tester
========================
Tests the actual Gemini connection used for dynamic RTA quips, and shows
you several real generated examples for a team -- without needing
Discord, GitHub Actions, or anything else. Directly reuses rta_logic.py's
real functions (get_team_recent_form, build_quip_prompt, the same model
chain), so what you see here is a genuine preview of what the live bot
would produce, not a separate approximation.

Usage:
    export GENAI_API_KEY=your_real_key
    python test_quips_offline.py Arkansas
    python test_quips_offline.py "South Carolina" --count 8
"""
import argparse
import os
import sys

import rta_logic as rl


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("team", help="Exact team name as it appears in your roster/schedule data")
    parser.add_argument("--count", type=int, default=5, help="How many example quips to generate (default 5)")
    args = parser.parse_args()

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

    form = rl.get_team_recent_form(args.team)
    if form is None:
        print(f"No completed-game data found for '{args.team}' -- can't build a quip yet.")
        print("(Check the team name matches exactly, and that dynasty_data_<season>.csv")
        print(" is in this same folder.)")
        sys.exit(1)

    print("=" * 70)
    print(f"Real stats being used for {args.team}:")
    print("=" * 70)
    for key, val in form.items():
        print(f"  {key}: {val}")
    print()

    client = genai.Client(api_key=api_key)
    style_counts = {}

    print("=" * 70)
    print(f"Generating {args.count} example quip(s) -- connecting to Gemini...")
    print("=" * 70)
    for i in range(1, args.count + 1):
        # Built fresh each time -- build_quip_prompt() randomly picks a
        # comedian style internally, same as the real bot does per reply.
        prompt = rl.build_quip_prompt(form)
        style_used = next((s["name"] for s in rl.COMEDIAN_STYLES if s["name"] in prompt), "unknown")

        try:
            response = client.models.generate_content(model=rl.QUIP_MODEL_CHAIN[0], contents=[prompt])
            text = (response.text or "").strip()
            if text:
                print(f"[{i}] ({style_used}): {text}")
            else:
                print(f"[{i}] ({style_used}): -- empty response from the model --")
        except Exception as e:
            print(f"[{i}] ({style_used}): CONNECTION/API FAILED -- {type(e).__name__}: {e}")

        style_counts[style_used] = style_counts.get(style_used, 0) + 1

    print()
    print("=" * 70)
    print("Style distribution this run:", style_counts)
    print("(Random per call -- run again to see a different mix, or add")
    print(" --count with a higher number to see more of the spread.)")
    print("=" * 70)


if __name__ == "__main__":
    main()
