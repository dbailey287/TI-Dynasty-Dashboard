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

Every invocation does its real work immediately (RTA/advance detection,
resetting status, posting the advance announcement to #announcements).
The META notifications -- the "here's what happened" status line to
#bot-admin-alerts and the run log to #bot-admin-logs -- are BATCHED
instead of sent every 10 minutes: they accumulate quietly and get sent
as one digest at ~6am and ~6pm Eastern, covering everything since the
last digest. A genuine FAILURE always alerts immediately, bypassing the
batch -- routine "ran fine" noise is what gets batched, not problems.

Meant to run on a SCHEDULE (e.g. every 5-10 minutes via GitHub Actions --
see .github/workflows/rta_tracker.yml), not as a persistent listener.

Required environment variables:
    DISCORD_TOKEN                Bot token (same one everything else uses)
    RTA_MAIN_CHANNEL_ID           #26-mega-dynasty channel ID
    RTA_ADMIN_CHANNEL_ID          #monitored-admin-advance channel ID
    RTA_ANNOUNCE_CHANNEL_ID       #announcements channel ID(s) -- comma-separated
                                   for multiple channels, e.g. "111,222"

Optional:
    SUMMARY_CHANNEL_ID             #bot-admin-alerts (reused from the scraper setup)
    ADMIN_LOG_CHANNEL_ID           #bot-admin-logs (reused from the scraper setup)
    RTA_ADVANCE_KEYWORD           Defaults to "advance"
    RTA_STATE_FILE                 Defaults to rta_status.json
    RTA_DIGEST_STATE_FILE           Defaults to rta_notification_log.json
    RTA_DIGEST_HOURS                 Defaults to "6,18" (24h, Eastern)
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

import notify_utils as notify
import roster
import rta_logic as rl

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
MAIN_CHANNEL_ID = os.environ.get("RTA_MAIN_CHANNEL_ID")
ADMIN_CHANNEL_ID = os.environ.get("RTA_ADMIN_CHANNEL_ID")
ANNOUNCE_CHANNEL_IDS = rl.parse_channel_ids(os.environ.get("RTA_ANNOUNCE_CHANNEL_ID", ""))
SUMMARY_CHANNEL_ID = os.environ.get("SUMMARY_CHANNEL_ID")
ADMIN_LOG_CHANNEL_ID = os.environ.get("ADMIN_LOG_CHANNEL_ID")
DIGEST_STATE_FILE = os.environ.get("RTA_DIGEST_STATE_FILE", "rta_notification_log.json")
DIGEST_HOURS = {int(h) for h in os.environ.get("RTA_DIGEST_HOURS", "6,18").split(",")}
EASTERN = ZoneInfo("America/New_York")
ADVANCE_KEYWORD = os.environ.get("RTA_ADVANCE_KEYWORD", rl.DEFAULT_ADVANCE_KEYWORD)
STATE_FILE = os.environ.get("RTA_STATE_FILE", "rta_status.json")
# Optional -- only used for the week-3+ dynamic quips. If unset, RTA
# replies just always use the static tagline bank, same as before this
# feature existed. Not in the required-vars check below on purpose.
GEMINI_API_KEY = os.environ.get("GENAI_API_KEY")

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


def reply_to_message(channel_id: str, message_id: str, token: str, content: str):
    """Posts a native Discord reply (shows the 'replying to' UI) to a
    specific message, rather than a standalone message."""
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    resp = requests.post(
        f"{API_BASE}/channels/{channel_id}/messages",
        headers=headers,
        json={"content": content, "message_reference": {"message_id": message_id}},
        timeout=15,
    )
    resp.raise_for_status()


DASHBOARD_URL = "https://ti-dynasty-dashboard-2027.streamlit.app/"

DEFAULT_DIGEST_STATE = {"pending_runs": [], "last_digest_sent_at": None}


def load_digest_state(path: str) -> dict:
    if not os.path.exists(path):
        return dict(DEFAULT_DIGEST_STATE)
    with open(path, "r") as f:
        state = json.load(f)
    for key, default in DEFAULT_DIGEST_STATE.items():
        state.setdefault(key, default)
    return state


def save_digest_state(path: str, state: dict):
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def should_send_digest_now(now_eastern: datetime, last_digest_sent_at: str) -> bool:
    """True if it's currently a digest hour AND we haven't already sent
    one recently -- the "recently" check is what stops this from firing
    on every single 10-minute tick within the target hour."""
    if now_eastern.hour not in DIGEST_HOURS:
        return False
    if not last_digest_sent_at:
        return True
    last_dt = datetime.fromisoformat(last_digest_sent_at)
    hours_since = (now_eastern.astimezone(timezone.utc) - last_dt.astimezone(timezone.utc)).total_seconds() / 3600
    return hours_since >= 1


def format_digest_alert(pending_runs: list) -> str:
    if not pending_runs:
        return "📋 RTA Tracker Digest — no runs recorded in this window."
    total_new_rta = sum(r.get("new_rta", 0) for r in pending_runs)
    advances = sum(1 for r in pending_runs if r.get("advance_triggered"))
    latest = pending_runs[-1]
    lines = [f"📋 **RTA Tracker Digest** — last ~12h ({len(pending_runs)} run(s))"]
    lines.append(f"{total_new_rta} new RTA(s), {advances} advance(s) triggered.")
    if "ready" in latest and "total" in latest:
        lines.append(f"Current: {latest['ready']}/{latest['total']} ready.")
    return "\n".join(lines)


def build_digest_log_text(pending_runs: list) -> str:
    parts = [f"=== {r.get('timestamp', '?')} ===\n{r.get('log_text', '')}" for r in pending_runs]
    return "\n\n".join(parts)


def post_chunked(channel_id: str, token: str, header: str, lines: list, max_len: int = 1900):
    """Posts header + lines as one message, splitting into multiple
    messages if it would exceed Discord's length limit (unlikely with 17
    teams, but safe regardless of roster size)."""
    chunk = header
    for line in lines:
        candidate = chunk + "\n" + line
        if len(candidate) > max_len:
            post_message(channel_id, token, chunk)
            chunk = line
        else:
            chunk = candidate
    if chunk:
        post_message(channel_id, token, chunk)


def run() -> str:
    """Returns a short status string for the alert channel. Raises on
    genuine failures -- the caller catches and reports those."""
    missing = [name for name, val in [
        ("DISCORD_TOKEN", DISCORD_TOKEN), ("RTA_MAIN_CHANNEL_ID", MAIN_CHANNEL_ID),
        ("RTA_ADMIN_CHANNEL_ID", ADMIN_CHANNEL_ID), ("RTA_ANNOUNCE_CHANNEL_ID", ANNOUNCE_CHANNEL_IDS),
    ] if not val]
    if missing:
        raise RuntimeError(f"Missing environment variable(s): {', '.join(missing)}")

    roster_entries = rl.load_active_roster(".")
    if not roster_entries:
        raise RuntimeError("Couldn't find/load Server_Members_Teams.csv (via roster.py).")
    tracked_ids = {r["user_id"] for r in roster_entries}
    id_to_team = {r["user_id"]: r["team"] for r in roster_entries}
    team_emoji = roster.load_team_emoji_map(".")
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

            offseason_idx = state.get("offseason_phase_index")

            if offseason_idx is not None:
                # Already mid-walk through the offseason phase sequence
                # (End of Season Recap -> ... -> Pre Season) -- none of
                # these correspond to real schedule/game data, so they're
                # tracked purely by this index, not the CSV-driven
                # week_sort numbering used the rest of the year.
                next_idx = offseason_idx + 1
                if next_idx < len(rl.OFFSEASON_PHASES):
                    state["offseason_phase_index"] = next_idx
                    label, reminder = rl.OFFSEASON_PHASES[next_idx]
                    announcement = f"📋 **{label}**"
                    if reminder:
                        announcement += f"\n{reminder}"
                    matchups = {}  # no games happen during the offseason walk
                    log.info("Offseason phase advance: '%s' (%d/%d).", label, next_idx + 1, len(rl.OFFSEASON_PHASES))
                else:
                    # Walked through every offseason phase -- the new
                    # season's Week 0 starts now.
                    state["offseason_phase_index"] = None
                    new_week_sort = 0
                    state["current_week_sort"] = new_week_sort
                    week_label = rl.get_week_label_for_sort(".", new_week_sort)
                    matchups = rl.get_matchups_for_week_sort(".", new_week_sort)
                    announcement = rl.pick_announcement() + f" We're now on **Week {week_label}**."
                    announcement += f"\n{rl.WEEK_ZERO_REMINDER}"
                    log.info("Offseason walk complete -- new season begins at Week %s.", week_label)
            else:
                was_bootstrap = state.get("current_week_sort") is None
                prior_week_sort = state.get("current_week_sort")
                if prior_week_sort is None:
                    # Bootstrap ONLY: no tracked week yet (e.g. very first
                    # advance ever), so fall back to the CSV's best guess this
                    # one time. Every advance after this walks SEASON_SEQUENCE
                    # from what we already know instead of re-deriving from
                    # the CSV, which is what avoids the staleness bug going
                    # forward.
                    prior_week_sort = rl.find_earliest_upcoming_week_sort(".")
                    if prior_week_sort is None:
                        prior_week_sort = 0

                # Season transition: if we were just on Bowl Week 4 (904 --
                # this is also when the National Championship happens,
                # confirmed the same real week/advance, not a separate one)
                # and the REAL bracket (never the predicted one) shows a
                # decided champion, this advance starts the offseason phase
                # walk (End of Season Recap through Pre Season) instead of
                # continuing the schedule-driven week numbering -- see the
                # offseason_idx branch above for what happens once that walk
                # finishes. Checking the bracket file rather than
                # dynasty_data is deliberate: dynasty_data only has games
                # for user-controlled teams, and it's entirely possible none
                # of them reach the National Championship, in which case
                # dynasty_data would never show it as complete even though
                # it genuinely happened.
                season_transition = False
                if prior_week_sort == 904:
                    current_season = rl.get_current_season(".")
                    if current_season is not None:
                        champion = rl.get_national_champion(current_season, ".")
                        if champion:
                            season_transition = True

                if season_transition:
                    state["offseason_phase_index"] = 0
                    label, reminder = rl.OFFSEASON_PHASES[0]
                    announcement = f"🎉 {champion} are your National Champions! The dynasty marches on.\n\n📋 **{label}**"
                    if reminder:
                        announcement += f"\n{reminder}"
                    matchups = {}
                    log.info("Season transition detected (champion=%s) -- starting offseason phase walk.", champion)
                else:
                    # Walks the explicit known sequence (regular season
                    # weeks 0-14, then Conf Champ, then Bowl 1-4) rather
                    # than blindly adding 1 -- a bare +1 would produce "15"
                    # after week 14 instead of jumping to Conf Champ (900),
                    # and would never land on any of the postseason's
                    # special values at all. Bootstrap is the one
                    # exception: that's a first-time SYNC to whatever week
                    # the CSV shows as current, not an advance past it.
                    if was_bootstrap:
                        new_week_sort = prior_week_sort
                    else:
                        new_week_sort = rl.next_in_season_sequence(prior_week_sort)
                    state["current_week_sort"] = new_week_sort
                    week_label = rl.get_week_label_for_sort(".", new_week_sort)
                    matchups = rl.get_matchups_for_week_sort(".", new_week_sort)
                    announcement = rl.pick_announcement()
                    announcement += f" We're now on **Week {week_label}**."
                    reminder = rl.WEEK_SORT_REMINDERS.get(new_week_sort)
                    if reminder:
                        announcement += f"\n{reminder}"
                    log.info("Advance detected -- posting to #announcements (week_sort=%s, label=%s).", new_week_sort, week_label)

            announcement += f"\n📊 {DASHBOARD_URL}"

            all_ids = [r["user_id"] for r in roster_entries]
            matchup_lines = rl.format_matchup_lines(all_ids, id_to_team, matchups, team_emoji) if matchups else []

            if matchup_lines:
                for cid in ANNOUNCE_CHANNEL_IDS:
                    post_chunked(
                        cid, DISCORD_TOKEN,
                        announcement + "\n\nThis week's matchups:", matchup_lines,
                    )
            else:
                for cid in ANNOUNCE_CHANNEL_IDS:
                    post_message(cid, DISCORD_TOKEN, announcement)

    main_messages = fetch_new_messages(MAIN_CHANNEL_ID, DISCORD_TOKEN, state.get("last_message_id_main"))
    log.info("Main channel: %d new message(s).", len(main_messages))
    new_rta_count = 0
    if main_messages:
        before = set(state.get("ready_user_ids", []))
        state, hits = rl.process_rta_messages(main_messages, tracked_ids, id_to_team, state)

        current_week_sort = state.get("current_week_sort")
        use_dynamic_quips = current_week_sort is not None and current_week_sort >= rl.DYNAMIC_QUIP_MIN_WEEK_SORT

        for hit in hits:
            team = hit["team"]
            reply_text = None
            source = None  # for logging: exactly why this reply is what it is

            if use_dynamic_quips:
                reply_text, used_prompt = rl.generate_dynamic_quip(team, GEMINI_API_KEY)
                if reply_text:
                    source = "DYNAMIC (Gemini)"
                    rl.log_quip_response(team, current_week_sort, reply_text, prompt=used_prompt)
                else:
                    source = "static (Gemini call failed or returned nothing -- check GENAI_API_KEY / API status)"
            else:
                source = f"static (week_sort={current_week_sort}, dynamic starts at {rl.DYNAMIC_QUIP_MIN_WEEK_SORT})"

            if not reply_text:
                # Either too early in the season for dynamic quips, or
                # the dynamic attempt didn't produce anything (missing
                # API key, API failure) -- fall back to the static bank.
                # Queue/last-used state only gets touched here, never
                # when a dynamic quip is actually used.
                last_used = state.get("last_tagline_by_team", {}).get(team)
                queue = state.get("tagline_queue_by_team", {}).get(team, [])
                reply_text, queue = rl.pick_tagline_round_robin(team, queue, last_used=last_used)
                if reply_text:
                    state.setdefault("tagline_queue_by_team", {})[team] = queue
                    state.setdefault("last_tagline_by_team", {})[team] = reply_text

            log.info("Reply for %s: %s -- %r", team, source, reply_text)
            # Posted immediately, NOT batched into the twice-daily digest --
            # unlike routine "ran fine" status, this is the one thing
            # worth seeing right away, since it's the only way to tell in
            # real time whether a given reply used a dynamic quip or fell
            # back to the static bank (and if static, exactly why).
            notify.post_alert(SUMMARY_CHANNEL_ID, DISCORD_TOKEN, f"🗨️ **{team}**: {source}\n> {reply_text}")

            if not reply_text:
                continue
            try:
                reply_to_message(MAIN_CHANNEL_ID, hit["message_id"], DISCORD_TOKEN, reply_text)
            except Exception as e:
                # A flavor-text reply failing is never worth treating as a
                # real run failure -- log it and keep going.
                log.warning("Couldn't reply with tagline for %s: %s", hit["team"], e)
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

    log_text = notify.LOG_BUFFER.getvalue()
    timestamp = datetime.now(timezone.utc).isoformat()

    if error_text:
        # Failures bypass the digest entirely and alert right away --
        # routine "ran fine" noise is what gets batched, not problems.
        notify.post_alert(SUMMARY_CHANNEL_ID, DISCORD_TOKEN, f"❌ RTA Tracker FAILED: {error_text}")
        notify.post_log_file(ADMIN_LOG_CHANNEL_ID, DISCORD_TOKEN, "rta_tracker")
        sys.exit(1)

    if status == "no_change":
        record = {"timestamp": timestamp, "new_rta": 0, "advance_triggered": False, "log_text": log_text}
    else:
        _, new_rta, advance, ready, total = status.split(":")
        record = {
            "timestamp": timestamp, "new_rta": int(new_rta), "advance_triggered": bool(int(advance)),
            "ready": int(ready), "total": int(total), "log_text": log_text,
        }

    # Full log posted every run, unconditionally -- not batched like the
    # short status digest below. With the tracker only actually firing
    # roughly hourly in practice (GitHub Actions scheduling being what it
    # is) rather than the configured ~10 minutes, and the channel muted
    # anyway, this is low-volume enough to just always post -- makes it
    # easy to grab a specific run's full output for troubleshooting
    # without waiting for or digging through a digest.
    notify.post_log_file(ADMIN_LOG_CHANNEL_ID, DISCORD_TOKEN, "rta_tracker")

    digest_state = load_digest_state(DIGEST_STATE_FILE)
    digest_state["pending_runs"].append(record)

    now_eastern = datetime.now(EASTERN)
    if should_send_digest_now(now_eastern, digest_state["last_digest_sent_at"]):
        alert = format_digest_alert(digest_state["pending_runs"])
        combined_log = build_digest_log_text(digest_state["pending_runs"])
        covered_count = len(digest_state["pending_runs"])
        notify.post_alert(SUMMARY_CHANNEL_ID, DISCORD_TOKEN, alert)
        notify.post_text_file(ADMIN_LOG_CHANNEL_ID, DISCORD_TOKEN, "rta_tracker_digest", combined_log)
        digest_state["pending_runs"] = []
        digest_state["last_digest_sent_at"] = now_eastern.astimezone(timezone.utc).isoformat()
        log.info("Digest sent (%d run(s) covered); pending_runs cleared.", covered_count)
    else:
        log.info("Not digest time -- accumulated, %d pending run(s).", len(digest_state["pending_runs"]))

    save_digest_state(DIGEST_STATE_FILE, digest_state)


if __name__ == "__main__":
    main()
