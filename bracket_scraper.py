"""
Playoff Bracket Scraper
========================
Reads College Football Playoff bracket screenshots posted in
#playoff-screenshots, uses Gemini vision to extract the full bracket
structure, and writes it to playoff_bracket_<season>.json.

Unlike schedule_scraper.py (which appends new rows to a growing CSV every
week), this script OVERWRITES the season's bracket file each run -- a new
bracket screenshot always shows the ENTIRE bracket as of that point (every
prior round's results plus the newest one), not just incremental new
data, so there's no need to merge row-by-row. Messages are processed
oldest-to-newest, so if more than one bracket screenshot is ever posted
in a single run, the most recent one naturally wins.

Meant to be triggered MANUALLY (workflow_dispatch only, no schedule) --
a bracket only updates a handful of times across an entire season
(First Round, Quarterfinal, Semifinal, National Championship), so a
recurring cron would spend almost all its runs doing nothing.

Required environment variables:
    DISCORD_TOKEN                  Bot token (same one everything else uses)
    GENAI_API_KEY                   Gemini API key
    BRACKET_CHANNEL_ID              #playoff-screenshots channel ID

Optional:
    SUMMARY_CHANNEL_ID              #bot-admin-alerts (reused from other scripts)
    ADMIN_LOG_CHANNEL_ID            #bot-admin-logs (reused from other scripts)
    SEASON                          Defaults to auto-detected from existing dynasty_data files
"""
import asyncio
import io
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

import discord
from google import genai
from google.genai.errors import APIError
from PIL import Image

import notify_utils as notify

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)-5s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("bracket_scraper")
notify.setup_log_capture()

BOT_TOKEN = os.environ.get("DISCORD_TOKEN")
GEMINI_API_KEY = os.environ.get("GENAI_API_KEY")
BRACKET_CHANNEL_ID = os.environ.get("BRACKET_CHANNEL_ID")
SUMMARY_CHANNEL_ID = os.environ.get("SUMMARY_CHANNEL_ID")
ADMIN_LOG_CHANNEL_ID = os.environ.get("ADMIN_LOG_CHANNEL_ID")

REQUIRED = [("DISCORD_TOKEN", BOT_TOKEN), ("GENAI_API_KEY", GEMINI_API_KEY), ("BRACKET_CHANNEL_ID", BRACKET_CHANNEL_ID)]
missing = [name for name, val in REQUIRED if not val]
if missing:
    log.error("Missing required environment variable(s): %s", ", ".join(missing))
    sys.exit(1)

MODEL_CHAIN = ["gemini-flash-lite-latest", "gemini-3.5-flash-lite", "gemini-flash-latest"]
RETRIES_PER_MODEL = 2
MAX_IMAGE_DIMENSION = 1024
STATE_FILE = "processed_bracket_images.json"
FAILED_FILE = "failed_bracket_images.json"

client = genai.Client(api_key=GEMINI_API_KEY)

VISION_PROMPT = """
Analyze this College Football Playoff bracket screenshot.
Extract the bracket in strict JSON format with these keys:
- "season": The year shown in the title (e.g. 2026), as an integer.
- "host_city": The city/state shown for the National Championship site (e.g. "Las Vegas, NV"), or "" if not visible.
- "champion": The team name shown on a "National Champions" banner, ONLY if that banner is visible (it only appears after the championship game is complete). Otherwise "".
- "rounds": An object with exactly four keys: "first_round", "quarterfinal", "semifinal", "national_championship".
  Each key's value is a list of game objects, one per bracket slot shown, each with:
    - "bowl": The bowl name for this specific game (e.g. "Cotton Bowl", "Peach Bowl", "Rose Bowl Game", "Capital One Orange Bowl", "Allstate Sugar Bowl"), or "" if no bowl name/logo is shown for that slot (First Round games typically don't have one).
    - "seed1": Integer seed of the first-listed (upper) team in that slot, or null if that slot is still empty/undetermined.
    - "team1": Name of the first-listed team, or "" if undetermined.
    - "score1": Integer score of the first-listed team, or null if not yet played.
    - "auto_qualifier1": true if team1's name has an asterisk (*) next to it, else false.
    - "seed2": Integer seed of the second-listed (lower) team in that slot, or null if undetermined.
    - "team2": Name of the second-listed team, or "" if undetermined.
    - "score2": Integer score of the second-listed team, or null if not yet played.
    - "auto_qualifier2": true if team2's name has an asterisk (*) next to it, else false.
Include EVERY game slot visible in the bracket image for all four rounds, even ones that haven't been played or determined yet (use null/"" for anything not yet visible, rather than omitting the slot).
Return ONLY raw JSON. No markdown formatting, no code fences, no commentary.
"""


def _strip_json_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _resize_for_vision(img: Image.Image) -> Image.Image:
    if max(img.size) <= MAX_IMAGE_DIMENSION:
        return img
    resized = img.copy()
    resized.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.LANCZOS)
    return resized


async def _call_model(model_name: str, img: Image.Image) -> str:
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: client.models.generate_content(model=model_name, contents=[VISION_PROMPT, img]),
    )
    return response.text


async def parse_bracket_image_with_vision(image_bytes: bytes, filename: str) -> dict | None:
    img = Image.open(io.BytesIO(image_bytes))
    img = _resize_for_vision(img)

    for model_name in MODEL_CHAIN:
        for attempt in range(1, RETRIES_PER_MODEL + 1):
            try:
                raw_text = await _call_model(model_name, img)
                cleaned = _strip_json_fences(raw_text)
                data = json.loads(cleaned)
                if "rounds" not in data:
                    raise ValueError("Response missing 'rounds' key")
                log.info("Parsed %s with %s (season=%s, champion=%s)", filename, model_name, data.get("season"), data.get("champion") or "(not yet decided)")
                return data
            except (APIError, json.JSONDecodeError, ValueError) as e:
                log.warning("Attempt %d with %s failed for %s: %s", attempt, model_name, filename, e)
                await asyncio.sleep(2 * attempt)
    log.error("All models/retries exhausted for %s", filename)
    return None


def load_processed_ids() -> set:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def save_processed_ids(ids: set) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(ids), f)


def load_failed_images() -> dict:
    if os.path.exists(FAILED_FILE):
        try:
            with open(FAILED_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_failed_images(failed: dict) -> None:
    with open(FAILED_FILE, "w") as f:
        json.dump(failed, f, indent=2)


def save_bracket(data: dict, season: int) -> str:
    path = f"playoff_bracket_{season}.json"
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return path


intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

_run_result = {"processed": 0, "failed": 0, "path": None, "champion": None}


@bot.event
async def on_ready():
    try:
        channel = bot.get_channel(int(BRACKET_CHANNEL_ID))
        if channel is None:
            log.error("Could not find channel %s -- check BRACKET_CHANNEL_ID and bot permissions.", BRACKET_CHANNEL_ID)
            await bot.close()
            return

        processed_ids = load_processed_ids()
        failed = load_failed_images()
        messages = [msg async for msg in channel.history(limit=50, oldest_first=True)]

        newly_processed = 0
        last_good_data = None
        for msg in messages:
            for attachment in msg.attachments:
                if not attachment.content_type or "image" not in attachment.content_type:
                    continue
                att_id = str(attachment.id)
                if att_id in processed_ids:
                    continue
                image_bytes = await attachment.read()
                data = await parse_bracket_image_with_vision(image_bytes, attachment.filename)
                if data is None:
                    failed[att_id] = {"filename": attachment.filename, "message_id": str(msg.id), "at": datetime.now(timezone.utc).isoformat()}
                    continue
                last_good_data = data
                processed_ids.add(att_id)
                newly_processed += 1

        if last_good_data:
            season = last_good_data.get("season") or datetime.now().year
            path = save_bracket(last_good_data, season)
            _run_result["path"] = path
            _run_result["champion"] = last_good_data.get("champion") or None

        _run_result["processed"] = newly_processed
        _run_result["failed"] = len(failed) - len(load_failed_images())  # net new failures this run
        save_processed_ids(processed_ids)
        save_failed_images(failed)
        log.info("Run complete: %d new image(s) processed, %d total failed images on record.", newly_processed, len(failed))
    except Exception:
        log.exception("Unhandled error during bracket scrape:")
        raise
    finally:
        await bot.close()


def main():
    error_text = None
    try:
        bot.run(BOT_TOKEN, log_handler=None)
    except Exception as e:
        error_text = f"{type(e).__name__}: {e}"
        log.exception("Bot run failed:")

    if error_text:
        notify.post_alert(SUMMARY_CHANNEL_ID, BOT_TOKEN, f"❌ Bracket Scraper FAILED: {error_text}")
        notify.post_log_file(ADMIN_LOG_CHANNEL_ID, BOT_TOKEN, "bracket_scraper")
        sys.exit(1)

    if _run_result["path"]:
        champ_note = f" 🏆 Champion: {_run_result['champion']}" if _run_result["champion"] else ""
        alert = f"✅ Bracket Scraper: updated {_run_result['path']} ({_run_result['processed']} new image(s)).{champ_note}"
    elif _run_result["processed"] == 0:
        alert = "✅ Bracket Scraper: ran, no new bracket screenshots found."
    else:
        alert = "⚠️ Bracket Scraper: ran, but no bracket was successfully extracted this time."

    notify.post_alert(SUMMARY_CHANNEL_ID, BOT_TOKEN, alert)
    notify.post_log_file(ADMIN_LOG_CHANNEL_ID, BOT_TOKEN, "bracket_scraper")
    sys.exit(0)


if __name__ == "__main__":
    main()
