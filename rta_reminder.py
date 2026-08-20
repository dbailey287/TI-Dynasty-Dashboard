"""
RTA reminder -- tags everyone who hasn't said RTA yet, on Sunday/
Wednesday/Friday mornings (your league's advance days), in
#announcements. Shows each person's current-week opponent when
available, and flags User vs User games specially.

The workflow triggers once a day, at one fixed UTC time (see
.github/workflows/rta_reminder.yml) -- no DST handling, since the exact
hour it lands at isn't important, only that it's morning and only fires
once. This script only double-checks that today is actually an advance
day, as a safety net (e.g. if a manual run happens on the wrong day).

Every invocation (including gated no-ops) posts:
  - a short status line to #bot-admin-alerts (SUMMARY_CHANNEL_ID)
  - the full run log as a file to #bot-admin-logs (ADMIN_LOG_CHANNEL_ID)

Required environment variables:
    DISCORD_TOKEN                Bot token
    RTA_MAIN_CHANNEL_ID           #26-mega-dynasty channel ID (for the
                                   clickable link in the reminder text)
    RTA_ANNOUNCE_CHANNEL_ID       #announcements channel ID(s) -- comma-separated
                                   for multiple channels, e.g. "111,222"

Optional:
    SUMMARY_CHANNEL_ID             #bot-admin-alerts (reused from the scraper setup)
    ADMIN_LOG_CHANNEL_ID           #bot-admin-logs (reused from the scraper setup)
    RTA_STATE_FILE                  Defaults to rta_status.json
    RTA_ADVANCE_DAYS                 Defaults to "Sunday,Wednesday,Friday"
    RTA_FORCE_RUN                     Set to "1" to skip the day gate
"""
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import notify_utils as notify
import rta_logic as rl

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
MAIN_CHANNEL_ID = os.environ.get("RTA_MAIN_CHANNEL_ID")
ANNOUNCE_CHANNEL_IDS = rl.parse_channel_ids(os.environ.get("RTA_ANNOUNCE_CHANNEL_ID", ""))
SUMMARY_CHANNEL_ID = os.environ.get("SUMMARY_CHANNEL_ID")
ADMIN_LOG_CHANNEL_ID = os.environ.get("ADMIN_LOG_CHANNEL_ID")
STATE_FILE = os.environ.get("RTA_STATE_FILE", "rta_status.json")
ADVANCE_DAYS = {d.strip().lower() for d in os.environ.get("RTA_ADVANCE_DAYS", "Sunday,Wednesday,Friday").split(",")}
FORCE_RUN = os.environ.get("RTA_FORCE_RUN") == "1"

API_BASE = "https://discord.com/api/v10"
EASTERN = ZoneInfo("America/New_York")
DASHBOARD_URL = "https://ti-dynasty-dashboard-2027.streamlit.app/"

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)-5s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("rta_reminder")
notify.setup_log_capture()


def should_run_now(now_eastern: datetime) -> bool:
    """Day-of-week only -- the exact hour doesn't matter, just that this
    fires once, on an actual advance day."""
    return now_eastern.strftime("%A").lower() in ADVANCE_DAYS


def post_message(channel_id: str, token: str, content: str):
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    resp = requests.post(
        f"{API_BASE}/channels/{channel_id}/messages",
        headers=headers, json={"content": content}, timeout=15,
    )
    resp.raise_for_status()


def format_reminder_message(not_ready_ids: list, id_to_team: dict, week_label: str,
                             matchups: dict, main_channel_id: str) -> str:
    intro = rl.pick_nag_intro().rstrip(".")
    if week_label != "?":
        intro += f" on **Week {week_label}**."
    else:
        intro += "."

    lines = [intro, "", "Still waiting on:"]
    lines.extend(rl.format_matchup_lines(not_ready_ids, id_to_team, matchups))

    lines.append("")
    if main_channel_id:
        lines.append(f"Drop an **RTA** in <#{main_channel_id}> when you're ready!")
    else:
        lines.append("Drop an **RTA** in #26-mega-dynasty when you're ready!")
    lines.append(f"📊 {DASHBOARD_URL}")

    return "\n".join(lines)


def run() -> str:
    """Returns a short status string describing what happened, for the
    alert channel. Raises on genuine failures (network errors etc.) --
    the caller is responsible for catching and reporting those."""
    now_eastern = datetime.now(EASTERN)
    log.info("Current Eastern time: %s", now_eastern.strftime("%A %Y-%m-%d %H:%M %Z"))

    if not FORCE_RUN and not should_run_now(now_eastern):
        log.info("Not an advance day -- no-op.")
        return "skipped_gate"

    missing = [name for name, val in [
        ("DISCORD_TOKEN", DISCORD_TOKEN), ("RTA_ANNOUNCE_CHANNEL_ID", ANNOUNCE_CHANNEL_IDS),
    ] if not val]
    if missing:
        raise RuntimeError(f"Missing environment variable(s): {', '.join(missing)}")

    roster = rl.load_active_roster(".")
    if not roster:
        raise RuntimeError("Couldn't find/load Server_Members_Teams.csv (via roster.py).")
    tracked_ids = {r["user_id"] for r in roster}
    id_to_team = {r["user_id"]: r["team"] for r in roster}

    state = rl.load_state(STATE_FILE)
    ready = set(state.get("ready_user_ids", []))
    not_ready = sorted(tracked_ids - ready)
    log.info("Ready: %d/%d. Not ready: %d.", len(ready & tracked_ids), len(tracked_ids), len(not_ready))

    if not not_ready:
        log.info("Everyone's ready -- nothing to nag about.")
        return "skipped_ready"

    week_label, matchups = rl.get_current_week_matchups(".")
    log.info("Week label: %s. Matchups found for %d team(s).", week_label, len(matchups))

    message = format_reminder_message(not_ready, id_to_team, week_label, matchups, MAIN_CHANNEL_ID)
    for cid in ANNOUNCE_CHANNEL_IDS:
        post_message(cid, DISCORD_TOKEN, message)
    log.info("Reminder posted (%d tagged).", len(not_ready))
    return f"sent:{len(not_ready)}"


def main():
    status = "failed"
    error_text = None
    try:
        status = run()
    except Exception as e:
        error_text = f"{type(e).__name__}: {e}"
        log.exception("Unhandled error in RTA reminder:")

    if status == "skipped_gate":
        alert = "⏭️ RTA Reminder: skipped (not an advance day)."
    elif status == "skipped_ready":
        alert = "✅ RTA Reminder: skipped, everyone was already RTA."
    elif status.startswith("sent:"):
        count = status.split(":", 1)[1]
        alert = f"✅ RTA Reminder: sent, tagged {count} not-yet-ready user(s)."
    else:
        alert = f"❌ RTA Reminder FAILED: {error_text}"

    notify.post_alert(SUMMARY_CHANNEL_ID, DISCORD_TOKEN, alert)
    notify.post_log_file(ADMIN_LOG_CHANNEL_ID, DISCORD_TOKEN, "rta_reminder")

    if error_text:
        sys.exit(1)


if __name__ == "__main__":
    main()
