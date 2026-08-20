"""
Recruiting Rankings Scraper
============================
Reads recruiting-rankings screenshots posted in #recruiting-rankings
(one screenshot per conference, showing national rank / star breakdown
/ NIL spend for each team's current recruiting class), uses Gemini
vision to extract every row, keeps ONLY user-controlled teams (per
roster.py), and writes/updates recruiting_ranks_<season>.csv.

Meant to be triggered MANUALLY (workflow_dispatch only, no schedule) --
this is a once-per-offseason upload, not something to poll for.

Each screenshot only shows part of a conference's standings (the
in-game leaderboard requires scrolling), so it's normal for multiple
screenshots to overlap on some rows across a single upload batch --
this is fine, since every row is upserted by (Season, Team), not
appended, so a team appearing in more than one screenshot just gets
overwritten with its own consistent values. Since only user teams are
kept at all, most conference screenshots contribute zero or one row
regardless of how many total teams they show.

This data is a season snapshot, not incremental like schedule rows --
one row per user team per season, always representing "recruiting as
of the moment these screenshots were taken." Re-running for the same
season with new screenshots (e.g. a corrected image) safely overwrites
just that team's row.

Season resolution:
    --season CLI flag (the workflow_dispatch input) wins if given.
    Otherwise defaults to (newest existing dynasty_data_<season>.csv's
    season) + 1, since a recruiting class always represents the
    UPCOMING season, not the one just finished. This is announced
    clearly in the run's summary post, never a silent guess.

Required environment variables:
    DISCORD_TOKEN                   Bot token (same one everything else uses)
    GENAI_API_KEY                   Gemini API key
    RECRUITING_CHANNEL_ID           #recruiting-rankings channel ID

Optional:
    SUMMARY_CHANNEL_ID              #bot-admin-alerts (reused from other scripts)
    ADMIN_LOG_CHANNEL_ID            #bot-admin-logs (reused from other scripts)

Usage:
    python recruiting_scraper.py --season 2027
    python recruiting_scraper.py                  -> auto-defaults, see above
"""
import asyncio
import csv
import glob
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
import roster

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)-5s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("recruiting_scraper")
notify.setup_log_capture()

BOT_TOKEN = os.environ.get("DISCORD_TOKEN")
GEMINI_API_KEY = os.environ.get("GENAI_API_KEY")
RECRUITING_CHANNEL_ID = os.environ.get("RECRUITING_CHANNEL_ID")
SUMMARY_CHANNEL_ID = os.environ.get("SUMMARY_CHANNEL_ID")
ADMIN_LOG_CHANNEL_ID = os.environ.get("ADMIN_LOG_CHANNEL_ID")

REQUIRED = [("DISCORD_TOKEN", BOT_TOKEN), ("GENAI_API_KEY", GEMINI_API_KEY), ("RECRUITING_CHANNEL_ID", RECRUITING_CHANNEL_ID)]
missing = [name for name, val in REQUIRED if not val]
if missing:
    log.error("Missing required environment variable(s): %s", ", ".join(missing))
    sys.exit(1)

MODEL_CHAIN = ["gemini-flash-lite-latest", "gemini-3.5-flash-lite", "gemini-flash-latest"]
RETRIES_PER_MODEL = 2
MAX_IMAGE_DIMENSION = 1024
STATE_FILE = "processed_recruiting_images.json"
FAILED_FILE = "failed_recruiting_images.json"
CSV_HEADER = [
    "Season", "Team", "Conference", "National_Rank", "Total_Commits", "NIL_Spent",
    "Five_Star", "Four_Star", "Three_Star", "Two_Star", "One_Star", "PTS",
]

client = genai.Client(api_key=GEMINI_API_KEY)

VISION_PROMPT = """
Analyze this college football recruiting rankings screenshot. It shows one
conference's teams with recruiting class stats.

Extract in strict JSON format with these keys:
- "conference": The conference name shown in the top-left box (e.g. "ACC", "SEC", "BIG 12"), as plain text.
- "teams": A list of objects, one per row visible in the table, each with:
    - "rank": The national rank number in the leftmost column, as an integer.
    - "team": The team name (e.g. "Miami", "Ohio State") -- just the school name, not any ranking number shown next to it in that cell.
    - "total_commits": The number in the TOTAL column, as an integer.
    - "nil_spent": The number in the NIL column (ignore the diamond icon), as an integer.
    - "five_star": The number in the 5-STAR column, as an integer.
    - "four_star": The number in the 4-STAR column, as an integer.
    - "three_star": The number in the 3-STAR column, as an integer.
    - "two_star": The number in the 2-STAR column, as an integer.
    - "one_star": The number in the 1-STAR column, as an integer.
    - "pts": The number in the PTS column (rightmost, may be partially cut off -- give your best read), as a number (can have a decimal).
Include EVERY row visible in the table, even if the screenshot is scrolled to show only part of the full conference list.
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


async def parse_recruiting_image_with_vision(image_bytes: bytes, filename: str) -> dict | None:
    img = Image.open(io.BytesIO(image_bytes))
    img = _resize_for_vision(img)

    for model_name in MODEL_CHAIN:
        for attempt in range(1, RETRIES_PER_MODEL + 1):
            try:
                raw_text = await _call_model(model_name, img)
                cleaned = _strip_json_fences(raw_text)
                data = json.loads(cleaned)
                if "teams" not in data:
                    raise ValueError("Response missing 'teams' key")
                log.info("Parsed %s with %s (conference=%s, %d row(s))", filename, model_name, data.get("conference"), len(data["teams"]))
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


def get_current_season(directory: str = ".") -> int | None:
    """Returns the season number of the newest dynasty_data_<season>.csv
    found, or None if there isn't one yet. Deliberately duplicated here
    (rather than importing rta_logic.py) to keep this scraper standalone
    -- same reasoning schedule_scraper.py and bracket_scraper.py don't
    depend on each other or on rta_logic.py either."""
    files = glob.glob(os.path.join(directory, "dynasty_data_*.csv"))
    if not files:
        return None

    def season_num(path):
        m = re.search(r"dynasty_data_(\d+)\.csv$", os.path.basename(path))
        return int(m.group(1)) if m else -1
    latest = max(files, key=season_num)
    n = season_num(latest)
    return n if n != -1 else None


def resolve_season(directory: str = ".") -> tuple[int, bool]:
    """Returns (season, was_explicit). --season CLI flag wins if given;
    otherwise defaults to get_current_season()+1 (a recruiting class
    always represents the UPCOMING season). Falls back to the current
    calendar year only if there's no dynasty_data file at all yet to
    infer from."""
    for i, arg in enumerate(sys.argv):
        if arg == "--season" and i + 1 < len(sys.argv):
            return int(sys.argv[i + 1]), True

    current = get_current_season(directory)
    if current is not None:
        return current + 1, False
    return datetime.now().year, False


def load_user_teams(directory: str = ".") -> set:
    """Every team currently controlled by an active user, per
    roster.py -- everything else gets dropped during parsing."""
    path = roster.find_roster_csv(directory)
    if path is None:
        log.warning("No Server_Members_Teams.csv found -- can't filter to user teams, nothing will be kept.")
        return set()
    return {r["team"] for r in roster.load_roster(path)}


def upsert_rows(season: int, new_rows: list[dict], directory: str = ".") -> int:
    """Merges new_rows into recruiting_ranks_<season>.csv, keyed on
    (Season, Team) -- a team appearing in more than one screenshot this
    run (or reappearing on a rerun) just overwrites its own row rather
    than duplicating. Returns the number of rows in the final file."""
    path = os.path.join(directory, f"recruiting_ranks_{season}.csv")
    existing = {}
    if os.path.exists(path):
        with open(path, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing[row["Team"]] = row

    for row in new_rows:
        existing[row["Team"]] = row

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
        writer.writeheader()
        for team in sorted(existing.keys()):
            writer.writerow(existing[team])

    return len(existing)


intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

_run_result = {"processed": 0, "kept_rows": 0, "total_rows_in_file": 0, "season": None, "season_was_explicit": None, "path": None}


@bot.event
async def on_ready():
    try:
        channel = bot.get_channel(int(RECRUITING_CHANNEL_ID))
        if channel is None:
            log.error("Could not find channel %s -- check RECRUITING_CHANNEL_ID and bot permissions.", RECRUITING_CHANNEL_ID)
            await bot.close()
            return

        season, was_explicit = resolve_season()
        _run_result["season"] = season
        _run_result["season_was_explicit"] = was_explicit
        log.info("Season for this run: %d (%s)", season, "explicit --season flag" if was_explicit else "auto-defaulted from existing data")

        user_teams = load_user_teams()
        log.info("Tracking %d user-controlled team(s).", len(user_teams))

        processed_ids = load_processed_ids()
        failed = load_failed_images()
        messages = [msg async for msg in channel.history(limit=50, oldest_first=True)]

        newly_processed = 0
        all_kept_rows = []
        for msg in messages:
            for attachment in msg.attachments:
                if not attachment.content_type or "image" not in attachment.content_type:
                    continue
                att_id = str(attachment.id)
                if att_id in processed_ids:
                    continue
                image_bytes = await attachment.read()
                data = await parse_recruiting_image_with_vision(image_bytes, attachment.filename)
                if data is None:
                    failed[att_id] = {"filename": attachment.filename, "message_id": str(msg.id), "at": datetime.now(timezone.utc).isoformat()}
                    continue

                conference = data.get("conference", "")
                for team_row in data.get("teams", []):
                    team_name = team_row.get("team", "").strip()
                    if team_name not in user_teams:
                        continue
                    all_kept_rows.append({
                        "Season": season,
                        "Team": team_name,
                        "Conference": conference,
                        "National_Rank": team_row.get("rank", ""),
                        "Total_Commits": team_row.get("total_commits", ""),
                        "NIL_Spent": team_row.get("nil_spent", ""),
                        "Five_Star": team_row.get("five_star", ""),
                        "Four_Star": team_row.get("four_star", ""),
                        "Three_Star": team_row.get("three_star", ""),
                        "Two_Star": team_row.get("two_star", ""),
                        "One_Star": team_row.get("one_star", ""),
                        "PTS": team_row.get("pts", ""),
                    })

                processed_ids.add(att_id)
                failed.pop(att_id, None)
                newly_processed += 1

        if all_kept_rows:
            total_in_file = upsert_rows(season, all_kept_rows)
            _run_result["path"] = f"recruiting_ranks_{season}.csv"
            _run_result["total_rows_in_file"] = total_in_file

        _run_result["processed"] = newly_processed
        _run_result["kept_rows"] = len(all_kept_rows)
        save_processed_ids(processed_ids)
        save_failed_images(failed)
        log.info("Run complete: %d image(s) processed, %d user-team row(s) kept, %d total failed images on record.", newly_processed, len(all_kept_rows), len(failed))
    except Exception:
        log.exception("Unhandled error during recruiting scrape:")
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
        notify.post_alert(SUMMARY_CHANNEL_ID, BOT_TOKEN, f"❌ Recruiting Scraper FAILED: {error_text}")
        notify.post_log_file(ADMIN_LOG_CHANNEL_ID, BOT_TOKEN, "recruiting_scraper")
        sys.exit(1)

    season = _run_result["season"]
    season_note = "" if _run_result["season_was_explicit"] else f" (auto-defaulted to {season})"
    if _run_result["path"]:
        alert = (
            f"✅ Recruiting Scraper: season {season}{season_note} -- "
            f"{_run_result['kept_rows']} user-team row(s) updated from {_run_result['processed']} image(s). "
            f"{_run_result['total_rows_in_file']} total row(s) now in {_run_result['path']}."
        )
    elif _run_result["processed"] == 0:
        alert = "✅ Recruiting Scraper: ran, no new recruiting screenshots found."
    else:
        alert = f"⚠️ Recruiting Scraper: processed {_run_result['processed']} image(s) but found no user-team rows to keep -- check RECRUITING_CHANNEL_ID and roster.py."

    notify.post_alert(SUMMARY_CHANNEL_ID, BOT_TOKEN, alert)
    notify.post_log_file(ADMIN_LOG_CHANNEL_ID, BOT_TOKEN, "recruiting_scraper")
    sys.exit(0)


if __name__ == "__main__":
    main()
