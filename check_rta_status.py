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

Every invocation posts:
  - a short status line to #bot-admin-alerts (SUMMARY_CHANNEL_ID)
  - the full run log as a file to #bot-admin-logs (ADMIN_LOG_CHANNEL_ID)

Meant to run on a SCHEDULE (e.g. every 5-10 minutes via GitHub Actions --
see .github/workflows/rta_tracker.yml), not as a persistent listener.

Required environment variables:
    DISCORD_TOKEN                Bot token (same one everything else uses)
    RTA_MAIN_CHANNEL_ID           #26-mega-dynasty channel ID
    RTA_ADMIN_CHANNEL_ID          #monitored-admin-advance channel ID
    RTA_ANNOUNCE_CHANNEL_ID       #announcements channel ID

Optional:
    SUMMARY_CHANNEL_ID             #bot-admin-alerts (reused from the scraper setup)
    ADMIN_LOG_CHANNEL_ID           #bot-admin-logs (reused from the scraper setup)
    RTA_ADVANCE_KEYWORD           Defaults to "advance"
    RTA_STATE_FILE                 Defaults to rta_status.json
"""
import logging
import os
import sys

import requests

import notify_utils as notify
import rta_logic as rl

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
MAIN_CHANNEL_ID = os.environ.get("RTA_MAIN_CHANNEL_ID")
ADMIN_CHANNEL_ID = os.environ.get("RTA_ADMIN_CHANNEL_ID")
ANNOUNCE_CHANNEL_ID = os.environ.get("RTA_ANNOUNCE_CHANNEL_ID")
SUMMARY_CHANNEL_ID = os.environ.get("SUMMARY_CHANNEL_ID")
ADMIN_LOG_CHANNEL_ID = os.environ.get("ADMIN_LOG_CHANNEL_ID")
ADVANCE_KEYWORD = os.environ.get("RTA_ADVANCE_KEYWORD", rl.DEFAULT_ADVANCE_KEYWORD)
STATE_FILE = os.environ.get("RTA_STATE_FILE", "rta_status.json")

API_BASE = "https://discord.com/api/v10"

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)-5s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("rta_tracker")
notify.setup_log_capture()


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


def run() -> str:
    """Returns a short status string for the alert channel. Raises on
    genuine failures -- the caller catches and reports those."""
    missing = [name for name, val in [
        ("DISCORD_TOKEN", DISCORD_TOKEN), ("RTA_MAIN_CHANNEL_ID", MAIN_CHANNEL_ID),
        ("RTA_ADMIN_CHANNEL_ID", ADMIN_CHANNEL_ID), ("RTA_ANNOUNCE_CHANNEL_ID", ANNOUNCE_CHANNEL_ID),
    ] if not val]
    if missing:
        raise RuntimeError(f"Missing environment variable(s): {', '.join(missing)}")

    roster = rl.load_active_roster(".")
    if not roster:
        raise RuntimeError("Couldn't find/load Server_Members_Teams.csv (via roster.py).")
    tracked_ids = {r["user_id"] for r in roster}
    log.info("Tracking %d active roster member(s).", len(tracked_ids))

    state = rl.load_state(STATE_FILE)
    changed = False
    advance_triggered = False

    admin_messages = fetch_new_messages(ADMIN_CHANNEL_ID, DISCORD_TOKEN, state.get("last_message_id_admin"))
    log.info("Admin channel: %d new message(s).", len(admin_messages))
    if admin_messages:
        state, triggered = rl.process_admin_messages(admin_messages, ADVANCE_KEYWORD, state)
        changed = True
        if triggered:
            advance_triggered = True
            week_label = rl.find_current_week_label(".")
            announcement = rl.pick_announcement()
            week_note = f" We're now on **Week {week_label}**." if week_label != "?" else ""
            log.info("Advance detected -- posting to #announcements (week=%s).", week_label)
            post_message(ANNOUNCE_CHANNEL_ID, DISCORD_TOKEN, announcement + week_note)

    main_messages = fetch_new_messages(MAIN_CHANNEL_ID, DISCORD_TOKEN, state.get("last_message_id_main"))
    log.info("Main channel: %d new message(s).", len(main_messages))
    new_rta_count = 0
    if main_messages:
        before = set(state.get("ready_user_ids", []))
        state = rl.process_rta_messages(main_messages, tracked_ids, state)
        new_rta_count = len(set(state["ready_user_ids"]) - before)
        changed = True

    if not changed:
        log.info("Nothing new in either channel -- state file unchanged.")
        return "no_change"

    rl.save_state(STATE_FILE, state)
    log.info("Ready: %d/%d", len(state["ready_user_ids"]), len(tracked_ids))
    return f"ok:{new_rta_count}:{int(advance_triggered)}:{len(state['ready_user_ids'])}:{len(tracked_ids)}"


def main():
    status = "failed"
    error_text = None
    try:
        status = run()
    except Exception as e:
        error_text = f"{type(e).__name__}: {e}"
        log.exception("Unhandled error during RTA tracker run:")

    if error_text:
        alert = f"❌ RTA Tracker FAILED: {error_text}"
    elif status == "no_change":
        alert = "✅ RTA Tracker: ran, nothing new."
    else:
        _, new_rta, advance, ready, total = status.split(":")
        parts = []
        if int(new_rta) > 0:
            parts.append(f"{new_rta} new RTA(s)")
        if int(advance):
            parts.append("advance triggered")
        detail = ", ".join(parts) if parts else "no new activity"
        alert = f"✅ RTA Tracker: ran ({detail}). Ready: {ready}/{total}."

    notify.post_alert(SUMMARY_CHANNEL_ID, DISCORD_TOKEN, alert)
    notify.post_log_file(ADMIN_LOG_CHANNEL_ID, DISCORD_TOKEN, "rta_tracker")

    if error_text:
        sys.exit(1)


if __name__ == "__main__":
    main()
