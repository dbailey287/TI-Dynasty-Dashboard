"""
Shared Discord notification helpers -- log capture + file posting, and a
short success/failure alert. Used by check_rta_status.py and
rta_reminder.py so both report consistently to #bot-admin-logs (full
detail, every run) and #bot-admin-alerts (short status, every run).

Uses raw REST calls (not discord.py), since these scripts don't maintain
a live gateway connection -- they run, do one thing, and exit.
"""
import datetime
import io
import json
import logging

import requests

API_BASE = "https://discord.com/api/v10"

# Captures every log line into memory so the full run log can be posted
# as a real file, not just whatever fits in a short Discord message.
LOG_BUFFER = io.StringIO()


def setup_log_capture(level=logging.DEBUG):
    handler = logging.StreamHandler(LOG_BUFFER)
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)-5s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    ))
    handler.setLevel(level)
    logging.getLogger().addHandler(handler)


def _post(channel_id: str, token: str, content: str = None,
          file_bytes: bytes = None, filename: str = None):
    headers = {"Authorization": f"Bot {token}"}
    if file_bytes is not None:
        files = {"file": (filename or "log.txt", file_bytes)}
        data = {"payload_json": json.dumps({"content": content} if content else {})}
        resp = requests.post(
            f"{API_BASE}/channels/{channel_id}/messages",
            headers=headers, data=data, files=files, timeout=20,
        )
    else:
        headers["Content-Type"] = "application/json"
        resp = requests.post(
            f"{API_BASE}/channels/{channel_id}/messages",
            headers=headers, json={"content": content}, timeout=15,
        )
    resp.raise_for_status()


def post_log_file(channel_id: str, token: str, script_name: str):
    """Posts the full captured run log as a .txt file. No-ops if
    channel_id isn't set; logs (doesn't raise) if the post itself fails,
    so a Discord hiccup here never crashes the actual automation."""
    if not channel_id:
        return
    log_text = LOG_BUFFER.getvalue() or "(no log output captured)"
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    try:
        _post(channel_id, token, file_bytes=log_text.encode("utf-8"),
              filename=f"{script_name}_{timestamp}.txt")
    except Exception as e:
        logging.getLogger(__name__).error("Failed to post log file: %s", e)


def post_alert(channel_id: str, token: str, message: str):
    """Posts a short status line. No-ops if channel_id isn't set."""
    if not channel_id:
        return
    try:
        _post(channel_id, token, content=message)
    except Exception as e:
        logging.getLogger(__name__).error("Failed to post alert: %s", e)
