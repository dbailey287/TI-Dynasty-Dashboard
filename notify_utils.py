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


def post_text_file(channel_id: str, token: str, script_name: str, text: str, content: str = None):
    """Posts arbitrary text as a .txt file (e.g. a combined multi-run
    digest log, not just the current process's own captured output)."""
    if not channel_id:
        return
    text = text or "(no log output captured)"
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    try:
        _post(channel_id, token, content=content, file_bytes=text.encode("utf-8"),
              filename=f"{script_name}_{timestamp}.txt")
    except Exception as e:
        logging.getLogger(__name__).error("Failed to post log file: %s", e)


def post_log_file(channel_id: str, token: str, script_name: str):
    """Posts the full captured run log (this process's own output) as a
    .txt file. No-ops if channel_id isn't set."""
    post_text_file(channel_id, token, script_name, LOG_BUFFER.getvalue())


def post_alert(channel_id: str, token: str, message: str):
    """Posts a short status line. No-ops if channel_id isn't set."""
    if not channel_id:
        return
    try:
        _post(channel_id, token, content=message)
    except Exception as e:
        logging.getLogger(__name__).error("Failed to post alert: %s", e)
