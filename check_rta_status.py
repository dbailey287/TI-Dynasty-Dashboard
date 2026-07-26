"""
RTA / Advance tracker.

Polls TWO Discord channels since the last check:
  - #26-mega-dynasty (main chat): looks for the word "RTA" from any
    ACTIVE roster member (matched by Discord user ID, from
    Server_Members_Teams.csv via roster.py)
  - #monitored-admin-advance: looks for "advance"/"advanced". Anyone who
    can post in that channel can trigger it -- Discord's own channel
    permissions are the security boundary here, not a username check.

On an advance: resets everyone's RTA status and posts a celebratory
message (plus the newly-detected week number, best-effort) to
#announcements.

Meant to run on a SCHEDULE (e.g. every 5-10 minutes via GitHub Actions --
see .github/workflows/rta_tracker.yml), not as a persistent listener.

Required environment variables:
    DISCORD_TOKEN                Bot token (same one everything else uses)
    RTA_MAIN_CHANNEL_ID           #26-mega-dynasty channel ID
    RTA_ADMIN_CHANNEL_ID          #monitored-admin-advance channel ID
    RTA_ANNOUNCE_CHANNEL_ID       #announcements channel ID

Optional:
    RTA_ADVANCE_KEYWORD           Defaults to "advance"
    RTA_STATE_FILE                 Defaults to rta_status.json
"""
import os
import sys

import requests

import rta_logic as rl

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
MAIN_CHANNEL_ID = os.environ.get("RTA_MAIN_CHANNEL_ID")
ADMIN_CHANNEL_ID = os.environ.get("RTA_ADMIN_CHANNEL_ID")
ANNOUNCE_CHANNEL_ID = os.environ.get("RTA_ANNOUNCE_CHANNEL_ID")
ADVANCE_KEYWORD = os.environ.get("RTA_ADVANCE_KEYWORD", rl.DEFAULT_ADVANCE_KEYWORD)
STATE_FILE = os.environ.get("RTA_STATE_FILE", "rta_status.json")

API_BASE = "https://discord.com/api/v10"


def fetch_new_messages(channel_id: str, token: str, after_id: str) -> list:
    """Fetches every message newer than after_id, oldest-first, paginating
    in batches of 100 if there's a backlog. Includes each message's
    author ID, not just username."""
    headers = {"Authorization": f"Bot {token}"}
    all_messages = []
    cursor = after_id

    while True:
        params = {"limit": 100}
        if cursor:
            params["after"] = cursor
        resp = requests.get(
            f"{API_BASE}/channels/{channel_id}/messages",
            headers=headers, params=params, timeout=15,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        batch.sort(key=lambda m: int(m["id"]))
        all_messages.extend(batch)
        cursor = batch[-1]["id"]
        if len(batch) < 100:
            break

    return [
        {"id": m["id"], "author_id": m["author"]["id"], "content": m.get("content", "")}
        for m in all_messages
    ]


def post_message(channel_id: str, token: str, content: str):
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    resp = requests.post(
        f"{API_BASE}/channels/{channel_id}/messages",
        headers=headers, json={"content": content}, timeout=15,
    )
    resp.raise_for_status()


def main():
    missing = [name for name, val in [
        ("DISCORD_TOKEN", DISCORD_TOKEN), ("RTA_MAIN_CHANNEL_ID", MAIN_CHANNEL_ID),
        ("RTA_ADMIN_CHANNEL_ID", ADMIN_CHANNEL_ID), ("RTA_ANNOUNCE_CHANNEL_ID", ANNOUNCE_CHANNEL_ID),
    ] if not val]
    if missing:
        sys.exit(f"Missing environment variable(s): {', '.join(missing)}")

    roster = rl.load_active_roster(".")
    if not roster:
        sys.exit("Couldn't find/load Server_Members_Teams.csv (via roster.py).")
    tracked_ids = {r["user_id"] for r in roster}
    print(f"Tracking {len(tracked_ids)} active roster member(s).")

    state = rl.load_state(STATE_FILE)
    changed = False

    # Admin channel first, so a same-run advance correctly wipes
    # ready_user_ids before any fresh RTAs for the new week get applied.
    admin_messages = fetch_new_messages(ADMIN_CHANNEL_ID, DISCORD_TOKEN, state.get("last_message_id_admin"))
    print(f"Admin channel: {len(admin_messages)} new message(s).")
    if admin_messages:
        state, triggered = rl.process_admin_messages(admin_messages, ADVANCE_KEYWORD, state)
        changed = True
        if triggered:
            week_label = rl.find_current_week_label(".")
            announcement = rl.pick_announcement()
            week_note = f" We're now on **Week {week_label}**." if week_label != "?" else ""
            print(f"Advance detected -- posting to #announcements (week={week_label}).")
            post_message(ANNOUNCE_CHANNEL_ID, DISCORD_TOKEN, announcement + week_note)

    main_messages = fetch_new_messages(MAIN_CHANNEL_ID, DISCORD_TOKEN, state.get("last_message_id_main"))
    print(f"Main channel: {len(main_messages)} new message(s).")
    if main_messages:
        state = rl.process_rta_messages(main_messages, tracked_ids, state)
        changed = True

    if not changed:
        print("Nothing new in either channel -- state file unchanged.")
        return

    rl.save_state(STATE_FILE, state)
    print(f"Ready: {len(state['ready_user_ids'])}/{len(tracked_ids)}")


if __name__ == "__main__":
    main()
