"""
Team Emoji Uploader
======================
One-time (but safely re-runnable) setup script. Uploads each user
team's logo as a custom emoji to the Discord server, so weekly_update.py
and the RTA advance/reminder messages can show a logo next to a team
name in plain message text.

Scope is deliberately just the user-controlled teams (per
Server_Members_Teams.csv via roster.py), not every possible CPU opponent
that could ever show up in a Top 25 poll -- a free-tier Discord server
only gets 50 custom emoji slots, and the user roster (currently ~18)
fits comfortably while "every FBS team" (100+) would not.

Idempotent: checks the guild's existing emoji list first and skips any
team that already has one (matched by emoji name), so re-running this
after adding a new user team only uploads what's missing rather than
duplicating everything.

Logo images come from dynasty_logic.py's existing ESPN CDN mapping
(TEAM_ESPN_ID / logo_url()) -- same source the dashboard already uses,
so there's no new image hosting to maintain.

Writes team_emoji_map.json: {team_name: "<:emoji_name:emoji_id>"} --
the full ready-to-embed markup, not just the ID. Consumed via
roster.load_team_emoji_map().

Required environment variables:
    DISCORD_TOKEN          Bot token (needs "Manage Expressions" permission)
    DISCORD_GUILD_ID        Server (guild) ID to upload emoji into

Usage:
    python upload_team_emoji.py
"""
import base64
import json
import logging
import os
import re
import sys
import time

import requests

import dynasty_logic as dl
import roster

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)-5s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("upload_team_emoji")

API_BASE = "https://discord.com/api/v10"

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("DISCORD_GUILD_ID")

MAX_IMAGE_BYTES = 256 * 1024  # Discord's hard limit per custom emoji


def emoji_name_for(team: str) -> str:
    """Discord emoji names: 2-32 chars, letters/digits/underscores only."""
    name = re.sub(r"[^a-zA-Z0-9_]", "_", team.lower()).strip("_")
    name = re.sub(r"_+", "_", name)
    return name[:32] or "team"


def get_existing_emoji(guild_id: str, token: str) -> dict:
    """Returns {emoji_name: emoji_id} for whatever's already on the server."""
    headers = {"Authorization": f"Bot {token}"}
    resp = requests.get(f"{API_BASE}/guilds/{guild_id}/emojis", headers=headers, timeout=15)
    resp.raise_for_status()
    return {e["name"]: e["id"] for e in resp.json()}


def upload_emoji(guild_id: str, token: str, name: str, image_bytes: bytes, content_type: str) -> str:
    """Returns the new emoji's ID."""
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {"name": name, "image": f"data:{content_type};base64,{b64}"}
    resp = requests.post(f"{API_BASE}/guilds/{guild_id}/emojis", headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    return resp.json()["id"]


def main():
    missing = [name for name, val in [("DISCORD_TOKEN", DISCORD_TOKEN), ("DISCORD_GUILD_ID", GUILD_ID)] if not val]
    if missing:
        log.error("Missing environment variable(s): %s", ", ".join(missing))
        sys.exit(1)

    roster_path = roster.find_roster_csv(".")
    if not roster_path:
        log.error("No Server_Members_Teams.csv found -- can't determine user teams.")
        sys.exit(1)
    roster_entries = roster.load_roster(roster_path)
    user_teams = sorted({r["team"] for r in roster_entries})
    log.info("%d user team(s) to process.", len(user_teams))

    try:
        existing = get_existing_emoji(GUILD_ID, DISCORD_TOKEN)
    except requests.HTTPError as e:
        log.error("Couldn't list existing emoji (check DISCORD_GUILD_ID and bot permissions): %s", e)
        sys.exit(1)
    log.info("%d emoji already on the server.", len(existing))

    emoji_map = {}
    # Load whatever map already exists so a partial prior run doesn't get
    # thrown away -- teams already resolved (existing or freshly uploaded)
    # get merged into it rather than the file being rebuilt from scratch.
    if os.path.exists("team_emoji_map.json"):
        try:
            with open("team_emoji_map.json", "r", encoding="utf-8") as f:
                emoji_map = json.load(f)
        except (OSError, ValueError):
            emoji_map = {}

    uploaded, skipped, failed = 0, 0, 0
    for team in user_teams:
        name = emoji_name_for(team)

        if name in existing:
            emoji_map[team] = f"<:{name}:{existing[name]}>"
            skipped += 1
            log.info("[SKIP] %s -- emoji '%s' already exists.", team, name)
            continue

        url = dl.logo_url(team)
        if not url:
            log.warning("[NO LOGO] %s -- not in TEAM_ESPN_ID, skipping.", team)
            failed += 1
            continue

        try:
            img_resp = requests.get(url, timeout=15)
            img_resp.raise_for_status()
            image_bytes = img_resp.content
            content_type = img_resp.headers.get("Content-Type", "image/png")
        except requests.RequestException as e:
            log.error("[FAILED] %s -- couldn't download logo: %s", team, e)
            failed += 1
            continue

        if len(image_bytes) > MAX_IMAGE_BYTES:
            log.error("[FAILED] %s -- logo is %d bytes, over Discord's %d limit.", team, len(image_bytes), MAX_IMAGE_BYTES)
            failed += 1
            continue

        try:
            emoji_id = upload_emoji(GUILD_ID, DISCORD_TOKEN, name, image_bytes, content_type)
            emoji_map[team] = f"<:{name}:{emoji_id}>"
            uploaded += 1
            log.info("[UPLOADED] %s -> <:%s:%s>", team, name, emoji_id)
        except requests.HTTPError as e:
            log.error("[FAILED] %s -- Discord rejected the upload: %s", team, e)
            failed += 1
            continue

        # Discord's emoji-creation rate limit is tight -- a short pause
        # between uploads avoids tripping it on a full 18-team run.
        time.sleep(1.5)

    with open("team_emoji_map.json", "w", encoding="utf-8") as f:
        json.dump(emoji_map, f, indent=2, sort_keys=True)

    log.info("Done. %d uploaded, %d already existed, %d failed. Wrote team_emoji_map.json (%d team(s) total).",
              uploaded, skipped, failed, len(emoji_map))


if __name__ == "__main__":
    main()
