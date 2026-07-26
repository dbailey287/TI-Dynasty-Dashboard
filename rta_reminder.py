"""
RTA reminder -- tags everyone who hasn't said RTA yet, 8 AM Eastern on
Sunday/Wednesday/Friday (your league's advance days), in #announcements.

DST handling: GitHub Actions cron always runs in UTC, and Eastern shifts
between UTC-4 (EDT, summer) and UTC-5 (EST, winter) twice a year. Rather
than hardcode one UTC time that would silently drift an hour off twice a
year, the workflow triggers this script at BOTH 12:00 UTC and 13:00 UTC
every day -- one of those is always correct for 8 AM Eastern, whichever
DST is in effect. The script itself checks the actual current Eastern
time (via zoneinfo, which knows about DST) and only posts if it's really
8 AM AND today is actually Sunday/Wednesday/Friday; every other trigger
is a silent no-op. This means it can never mis-fire or double-fire,
regardless of the calendar.

Required environment variables:
    DISCORD_TOKEN               Bot token
    RTA_ANNOUNCE_CHANNEL_ID      #announcements channel ID

Optional:
    RTA_STATE_FILE                Defaults to rta_status.json
    RTA_ADVANCE_DAYS               Defaults to "Sunday,Wednesday,Friday"
    RTA_REMINDER_HOUR               Defaults to 8 (24h, Eastern)
    RTA_FORCE_RUN                   Set to "1" to skip the day/hour gate
                                     (for manual testing)
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import rta_logic as rl

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
ANNOUNCE_CHANNEL_ID = os.environ.get("RTA_ANNOUNCE_CHANNEL_ID")
STATE_FILE = os.environ.get("RTA_STATE_FILE", "rta_status.json")
ADVANCE_DAYS = {d.strip().lower() for d in os.environ.get("RTA_ADVANCE_DAYS", "Sunday,Wednesday,Friday").split(",")}
REMINDER_HOUR = int(os.environ.get("RTA_REMINDER_HOUR", "8"))
FORCE_RUN = os.environ.get("RTA_FORCE_RUN") == "1"

API_BASE = "https://discord.com/api/v10"
EASTERN = ZoneInfo("America/New_York")


def should_run_now(now_eastern: datetime) -> bool:
    day_ok = now_eastern.strftime("%A").lower() in ADVANCE_DAYS
    hour_ok = now_eastern.hour == REMINDER_HOUR
    return day_ok and hour_ok


def post_message(channel_id: str, token: str, content: str):
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    resp = requests.post(
        f"{API_BASE}/channels/{channel_id}/messages",
        headers=headers, json={"content": content}, timeout=15,
    )
    resp.raise_for_status()


def build_reminder_message(not_ready: list) -> str:
    mentions = " ".join(_roster_mention(uid) for uid in not_ready)
    return f"{rl.pick_nag_intro()}\n\nStill waiting on: {mentions}\n\nDrop an **RTA** when you're ready!"


def _roster_mention(user_id: str) -> str:
    return f"<@{user_id}>"


def main():
    now_eastern = datetime.now(EASTERN)
    print(f"Current Eastern time: {now_eastern.strftime('%A %Y-%m-%d %H:%M %Z')}")

    if not FORCE_RUN and not should_run_now(now_eastern):
        print("Not an advance day/hour -- no-op.")
        return

    missing = [name for name, val in [
        ("DISCORD_TOKEN", DISCORD_TOKEN), ("RTA_ANNOUNCE_CHANNEL_ID", ANNOUNCE_CHANNEL_ID),
    ] if not val]
    if missing:
        sys.exit(f"Missing environment variable(s): {', '.join(missing)}")

    roster = rl.load_active_roster(".")
    if not roster:
        sys.exit("Couldn't find/load Server_Members_Teams.csv (via roster.py).")
    tracked_ids = {r["user_id"] for r in roster}

    state = rl.load_state(STATE_FILE)
    ready = set(state.get("ready_user_ids", []))
    not_ready = sorted(tracked_ids - ready)

    print(f"Ready: {len(ready & tracked_ids)}/{len(tracked_ids)}. Not ready: {len(not_ready)}.")

    if not not_ready:
        print("Everyone's ready -- nothing to nag about.")
        return

    message = build_reminder_message(not_ready)
    post_message(ANNOUNCE_CHANNEL_ID, DISCORD_TOKEN, message)
    print("Reminder posted.")


if __name__ == "__main__":
    main()
