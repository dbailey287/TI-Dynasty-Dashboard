"""
Manual RTA status editor -- lets you view or update rta_status.json using
team names instead of raw Discord user IDs. Mainly useful for seeding
real-world status when starting the automation mid-stream (so people who
already told you they were ready before the bot was watching don't have
to re-post RTA), or for any one-off correction.

This edits the file LOCALLY -- after running it, commit and push
rta_status.json so the tracker, reminder, and dashboard all see the
update. (Or just let the next scheduled RTA Tracker run overwrite it if
you'd rather not push manually -- but note that run will only ADD to
whatever's already there, not remove, so if you need to mark someone as
NOT ready, that has to happen here.)

Usage:
    python seed_rta_status.py --show
    python seed_rta_status.py --ready "Arkansas,Baylor,Missouri"
    python seed_rta_status.py --not-ready "Temple"
    python seed_rta_status.py --ready-all
    python seed_rta_status.py --clear
"""
import argparse
import sys

import rta_logic as rl


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ready", help="Comma-separated team names to mark as RTA")
    parser.add_argument("--not-ready", help="Comma-separated team names to mark as NOT RTA")
    parser.add_argument("--ready-all", action="store_true", help="Mark every active team as RTA")
    parser.add_argument("--clear", action="store_true", help="Clear RTA status for everyone")
    parser.add_argument("--show", action="store_true", help="Just print current status, no changes")
    parser.add_argument("--state-file", default="rta_status.json")
    args = parser.parse_args()

    entries = rl.load_active_roster(".")
    if not entries:
        sys.exit("Couldn't find/load Server_Members_Teams.csv (via roster.py).")
    team_to_id = {e["team"]: e["user_id"] for e in entries}
    id_to_team = {e["user_id"]: e["team"] for e in entries}

    state = rl.load_state(args.state_file)
    ready = set(state.get("ready_user_ids", []))

    def resolve_teams(csv_names: str) -> list:
        teams = [t.strip() for t in csv_names.split(",") if t.strip()]
        unknown = [t for t in teams if t not in team_to_id]
        if unknown:
            sys.exit(
                f"Unknown team name(s): {', '.join(unknown)}.\n"
                f"Known teams: {', '.join(sorted(team_to_id))}"
            )
        return teams

    made_changes = False

    if args.clear:
        ready = set()
        made_changes = True
        print("Cleared -- nobody marked ready.")
    elif args.ready_all:
        ready = set(team_to_id.values())
        made_changes = True
        print("Marked all active teams as ready.")
    else:
        if args.ready:
            teams = resolve_teams(args.ready)
            for t in teams:
                ready.add(team_to_id[t])
            made_changes = True
            print(f"Marked ready: {', '.join(teams)}")
        if args.not_ready:
            teams = resolve_teams(args.not_ready)
            for t in teams:
                ready.discard(team_to_id[t])
            made_changes = True
            print(f"Marked NOT ready: {', '.join(teams)}")

    if made_changes:
        state["ready_user_ids"] = sorted(ready)
        rl.save_state(args.state_file, state)
        print(f"Saved to {args.state_file}.")
    elif not args.show:
        print("No changes specified -- showing current status only. "
              "(Use --ready, --not-ready, --ready-all, or --clear to change anything.)")

    print()
    ready_teams = sorted(id_to_team[uid] for uid in ready if uid in id_to_team)
    not_ready_teams = sorted(set(team_to_id) - set(ready_teams))
    print(f"Current status: {len(ready_teams)}/{len(team_to_id)} ready")
    print("  Ready:    ", ", ".join(ready_teams) if ready_teams else "(none)")
    print("  Not ready:", ", ".join(not_ready_teams) if not_ready_teams else "(none)")


if __name__ == "__main__":
    main()
