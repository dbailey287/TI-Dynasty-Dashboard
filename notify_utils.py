"""
Shared Discord notification helpers -- log capture + file posting, short
status alerts, and rich embed messages. Used by check_rta_status.py,
rta_reminder.py, and weekly_update.py so all three report consistently.

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


def _post(channel_id: str, token: str, content: str = None, embeds: list = None,
          allowed_mentions: dict = None, components: list = None, flags: int = None,
          file_bytes: bytes = None, filename: str = None):
    headers = {"Authorization": f"Bot {token}"}
    payload = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds
    if allowed_mentions is not None:
        payload["allowed_mentions"] = allowed_mentions
    if components is not None:
        payload["components"] = components
    if flags is not None:
        payload["flags"] = flags
    if file_bytes is not None:
        files = {"file": (filename or "log.txt", file_bytes)}
        data = {"payload_json": json.dumps(payload)}
        resp = requests.post(
            f"{API_BASE}/channels/{channel_id}/messages",
            headers=headers, data=data, files=files, timeout=20,
        )
    else:
        headers["Content-Type"] = "application/json"
        resp = requests.post(
            f"{API_BASE}/channels/{channel_id}/messages",
            headers=headers, json=payload, timeout=15,
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


def post_message(channel_id: str, token: str, content: str, allow_everyone_ping: bool = False):
    """Posts a plain-content message (no embed) -- unlike post_alert(),
    this is meant for content that may include real markdown (mentions,
    custom emoji), which only renders OUTSIDE a code block/backticks
    (Discord doesn't parse markdown inside backtick-fenced text, embed or
    not).

    allow_everyone_ping controls whether a literal "@everyone" in content
    actually notifies the channel: False (default) suppresses it via
    Discord's allowed_mentions even if the text is present. Only pass
    True on the one message that's actually meant to ping everyone --
    NOTE the bot's role also needs the "Mention @everyone, @here, and All
    Roles" permission in that channel, or the ping silently won't fire
    regardless of this flag."""
    if not channel_id:
        return
    allowed_mentions = {"parse": ["everyone"]} if allow_everyone_ping else {"parse": []}
    try:
        _post(channel_id, token, content=content, allowed_mentions=allowed_mentions)
    except Exception as e:
        logging.getLogger(__name__).error("Failed to post message: %s", e)


def post_embeds(channel_id: str, token: str, embeds: list, content: str = None):
    """Posts up to 10 rich embeds in a single message. Each embed dict
    follows Discord's embed object shape -- title/description/color/etc.
    (see https://discord.com/developers/docs/resources/message#embed-object).
    Embeds get a much bigger character budget than plain content (up to
    6000 combined vs. content's 2000), which is the whole reason to use
    them for anything table-like. No-ops if channel_id isn't set."""
    if not channel_id:
        return
    if not embeds:
        logging.getLogger(__name__).warning("post_embeds called with no embeds -- nothing sent.")
        return
    if len(embeds) > 10:
        logging.getLogger(__name__).warning("post_embeds got %d embeds, Discord allows max 10 -- truncating.", len(embeds))
        embeds = embeds[:10]
    try:
        _post(channel_id, token, content=content, embeds=embeds)
    except Exception as e:
        logging.getLogger(__name__).error("Failed to post embeds: %s", e)


COMPONENTS_V2_FLAG = 1 << 15  # IS_COMPONENTS_V2, per Discord's message flag reference


def post_components_v2(channel_id: str, token: str, components: list):
    """Posts a message using Discord's newer Components V2 layout system
    (Container/Section/TextDisplay/Thumbnail/Separator, etc.) instead of
    the classic content+embeds fields -- those two fields are NOT usable
    together with Components V2, per Discord's docs, hence the separate
    function rather than extending post_message()/post_embeds().

    components is the raw list of top-level component objects (dicts)
    matching Discord's schema -- this function doesn't build them, just
    sends them with the required IS_COMPONENTS_V2 flag set. See
    test_components_v2.py for an example of building a Container with
    Sections + Thumbnail accessories.

    This is genuinely newer/less battle-tested than the rest of this
    module -- built from Discord's documented schema, not verified
    end-to-end here (this project's dev environment can't reach
    discord.com to test a live post). Treat the first real run as the
    actual test.

    Unlike the other post_* helpers here, this one RE-RAISES on failure
    instead of swallowing it -- for a prototype you're actively
    evaluating, a silent no-op on error is worse than a visible one."""
    if not channel_id:
        return
    if not components:
        logging.getLogger(__name__).warning("post_components_v2 called with no components -- nothing sent.")
        return
    try:
        _post(channel_id, token, components=components, flags=COMPONENTS_V2_FLAG,
              allowed_mentions={"parse": []})
    except Exception as e:
        logging.getLogger(__name__).error("Failed to post Components V2 message: %s", e)
        raise
