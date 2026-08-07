"""
CFB 27 Dynasty schedule scraper — optimized.

Key changes vs. the original:
  1. Fixed the corrupted regex line that would raise a SyntaxError.
  2. Model fallback chain: if the primary model keeps 503'ing, fall through
     to a secondary model instead of retrying the same overloaded endpoint.
  3. Jittered exponential backoff (avoids thundering-herd retries).
  4. Respects a Retry-After value if the API error object provides one.
  5. Skips images that were already successfully parsed in a prior run
     (tracked via a small JSON state file keyed on attachment ID).
  6. Bounded concurrency (semaphore) instead of one-at-a-time + flat sleep.
  7. Uses the `logging` module instead of print() for timestamps/levels.
  8. Prompts for (or accepts --season) a season year and scopes the CSV
     plus all state files to it: dynasty_data_<season>.csv. An existing
     season's file gets updated in place; a new season year creates a
     fresh file automatically. Multiple seasons never mix in one file.
"""

import discord
from discord.ext import commands
import datetime
import pandas as pd
import os
import re
import io
import json
import random
import asyncio
import logging
import sys
from PIL import Image
from google import genai
from google.genai.errors import APIError

# Optional .env support -- if python-dotenv isn't installed, this is a no-op
# and BOT_TOKEN/GEMINI_API_KEY just need to already be set in the environment.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------
# 1. CONFIGURATION
# ---------------------------------------------------------
# SECURITY: never hardcode tokens/API keys in the script. These are read
# from environment variables (set as User variables on this machine, or in
# a local .env file that is NOT committed to source control):
#     DISCORD_TOKEN=...
#     GENAI_API_KEY=...
#     SCREENSHOT_CHANNEL_ID=1530195664531750922,1530195710904107202,...   (comma-separated)


def _parse_SCREENSHOT_CHANNEL_ID(raw: str) -> list[int]:
    parts = re.split(r"[,\s;]+", raw.strip())
    ids = []
    for p in parts:
        if not p:
            continue
        try:
            ids.append(int(p))
        except ValueError:
            raise SystemExit(f"SCREENSHOT_CHANNEL_ID contains a non-numeric value: {p!r}")
    return ids


BOT_TOKEN = os.environ.get("DISCORD_TOKEN")
GEMINI_API_KEY = os.environ.get("GENAI_API_KEY")
SCREENSHOT_CHANNEL_ID_RAW = os.environ.get("SCREENSHOT_CHANNEL_ID")

_missing = [name for name, val in [
    ("DISCORD_TOKEN", BOT_TOKEN), ("GENAI_API_KEY", GEMINI_API_KEY), ("SCREENSHOT_CHANNEL_ID", SCREENSHOT_CHANNEL_ID_RAW),
] if not val]
if _missing:
    raise SystemExit(
        f"Missing environment variable(s): {', '.join(_missing)}. "
        "Set them as User variables (or in a local .env file) before running."
    )

TARGET_SCREENSHOT_CHANNEL_ID = _parse_SCREENSHOT_CHANNEL_ID(SCREENSHOT_CHANNEL_ID_RAW)

# Optional: post a short "what happened" summary and a fuller admin log
# after each run. Both are optional and independent -- leave either
# unset to skip that notification entirely. Values must be single
# channel IDs (not comma-separated lists like SCREENSHOT_CHANNEL_ID).
SUMMARY_CHANNEL_ID = int(os.environ["SUMMARY_CHANNEL_ID"]) if os.environ.get("SUMMARY_CHANNEL_ID") else None
ADMIN_LOG_CHANNEL_ID = int(os.environ["ADMIN_LOG_CHANNEL_ID"]) if os.environ.get("ADMIN_LOG_CHANNEL_ID") else None

# Try the primary model first; if it 503s past its own retry budget,
# fall through to the next model in the list rather than giving up.
#
# Using Google-maintained ALIASES ("...-latest") instead of dated model
# names where possible, since this account has now hit two separate 404s
# from Google quietly retiring/restricting specific dated models mid-project
# (gemini-2.5-flash-lite most recently). Aliases auto-follow whichever
# model Google currently recommends for that tier, so this chain shouldn't
# need manual updates every time Google reshuffles the lineup.
#
# COST NOTE (per-model pricing varies by source/day -- treat as directional,
# not exact -- but the ordering below is consistent everywhere checked,
# July 2026):
#   gemini-flash-lite-latest  currently -> gemini-3.1-flash-lite (cheapest)
#   gemini-3.5-flash-lite     next cheapest tier, confirmed available to this key
#   gemini-flash-latest       currently -> gemini-3.5-flash (most capable/priciest of the three)
MODEL_CHAIN = ["gemini-flash-lite-latest", "gemini-3.5-flash-lite", "gemini-flash-latest"]

# Screenshots are usually 1500-2500px on a side straight from Discord.
# Vision models tokenize images in tiles based on pixel dimensions, so a
# full-resolution screenshot can cost several times more than a resized
# one for the exact same (very legible) UI text. 1024px is comfortably
# enough resolution to read a schedule screen's text/numbers accurately.
MAX_IMAGE_DIMENSION = 1024

RETRIES_PER_MODEL = 3          # retries on a given model before falling back
MAX_BACKOFF_SECONDS = 30       # cap so a single image can't stall the run forever
MAX_CONCURRENT_REQUESTS = 3    # simultaneous vision calls in flight

client = genai.Client(api_key=GEMINI_API_KEY)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)-5s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cfb_scraper")

# Temporarily verbose while diagnosing a hang-on-shutdown issue in CI: this
# surfaces discord.py's own internal gateway/session lifecycle messages
# (heartbeats, close handshake, reconnects) that our own log lines don't
# cover, without changing the level of our own "cfb_scraper" logger above.
logging.getLogger("discord").setLevel(logging.DEBUG)

# Captures a copy of every log line (ours and discord.py's) into memory so
# the full run log can be posted as a real file to #bot-admin-logs, not
# just a hand-formatted summary. The console still gets output as normal
# via logging.basicConfig above -- this is an additional handler, not a
# replacement.
LOG_BUFFER = io.StringIO()
_log_capture_handler = logging.StreamHandler(LOG_BUFFER)
_log_capture_handler.setFormatter(logging.Formatter(
    "[%(asctime)s] [%(levelname)-5s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
))
_log_capture_handler.setLevel(logging.DEBUG)
logging.getLogger().addHandler(_log_capture_handler)


def _validate_season(raw: str) -> int:
    try:
        season = int(str(raw).strip())
    except ValueError:
        raise ValueError(f"'{raw}' isn't a valid year.")
    if not (2000 <= season <= 2100):
        raise ValueError(f"'{season}' doesn't look like a plausible season year.")
    return season


def resolve_season() -> int:
    """
    Determines which season this run applies to: a --season CLI flag or
    SEASON env var wins (for unattended/scheduled runs); otherwise prompts
    interactively. The season drives which files this run reads/writes --
    dynasty_data_<season>.csv plus its own processed/failed state files --
    so 2026 runs always update the 2026 file and a first 2027 run creates
    a brand-new one, automatically.
    """
    for i, arg in enumerate(sys.argv):
        if arg == "--season" and i + 1 < len(sys.argv):
            try:
                return _validate_season(sys.argv[i + 1])
            except ValueError as e:
                raise SystemExit(str(e))

    env_season = os.environ.get("SEASON")
    if env_season:
        try:
            return _validate_season(env_season)
        except ValueError as e:
            raise SystemExit(str(e))

    while True:
        raw = input("Which season is this run for? (e.g. 2026): ").strip()
        try:
            return _validate_season(raw)
        except ValueError as e:
            print(f"  {e}")

import roster as _roster  # shared with the dashboard and RTA scripts -- see roster.py

# Emergency fallback ONLY -- used if Server_Members_Teams.csv can't be
# found at all, so a missing file doesn't hard-crash a run. Team/user
# matching now comes from that CSV going forward; this dict is not meant
# to be maintained anymore.
_FALLBACK_USER_TEAMS_2026 = {
    "Brian": "Arizona State", "Clemsontigers1": "Arkansas", "TigerBo413": "Baylor",
    "Ben": "California", "tigerbrave27": "Colorado", "Holdma Dix": "Missouri",
    "Chefkdh": "Northwestern", "bigdaddydoubles": "Oklahoma State", "aalexbailey": "Pittsburgh",
    "cfuller23": "SMU", "Clemson256": "South Carolina", "bearofswag": "Stanford",
    "reign_man34": "Temple", "son_of_beef": "Virginia", "Rooke1221": "Virginia Tech",
    "Garnet Blood": "West Virginia", "Whobedis": "Wisconsin",
}


def resolve_user_teams(season: int) -> dict:
    """Loads {username: team} from Server_Members_Teams.csv (via roster.py),
    filtered to active users with a team assigned. Falls back to a small
    hardcoded 2026 mapping -- with a loud warning -- only if that CSV is
    missing entirely, so a first run without it doesn't just crash."""
    roster_path = _roster.find_roster_csv(".")
    if not roster_path:
        log.warning(
            "No Server_Members_Teams.csv found -- falling back to a hardcoded "
            "2026 mapping. Add that CSV to the repo for accurate, up-to-date "
            "team/user matching (see roster.py)."
        )
        return dict(_FALLBACK_USER_TEAMS_2026)

    entries = _roster.load_roster(roster_path)
    mapping = _roster.username_to_team(entries)
    log.info("Loaded %d active user(s) from %s.", len(mapping), roster_path)
    return mapping

VISION_PROMPT = """
Analyze this College Football Team Schedule screenshot.
Extract the schedule in strict JSON format with keys "featured_team" and "schedule".
Rules:
1. "featured_team": Read this from the SMALL white/light selector box near the
   top-left of the screen, next to a small controller-button icon labeled "LT"
   (NOT the large team name/logo in the main header card above it). That header
   card shows the user's own coached team, which is NOT necessarily the team
   whose schedule this screenshot displays -- the small "LT" selector box is
   the reliable indicator of which team's schedule is actually shown here.
2. "schedule": List of objects with keys:
   - "week": Week string/number (e.g. "0", "1", "6", "12", "Conf Champ").
   - "date": Date string (e.g., "Sat, Sep 5") or "" if BYE.
   - "location": "Home" if "vs", "Away" if "at", or "-" if BYE.
   - "opponent_rank": Rank number string if present (e.g. "20", "19", "4"), or "-" if unranked/BYE.
   - "opponent": Exact opponent team name (e.g. "Tennessee", "Oregon State", "BYU", "Ohio State"). If BYE, use "BYE".
   - "outcome": "W" or "L" if game completed, or "-" if unplayed/BYE.
   - "team_score": Integer score of featured team, or null.
   - "opponent_score": Integer score of opponent team, or null.
Return ONLY raw JSON. No markdown formatting or code blocks.
"""


# ---------------------------------------------------------
# 2. STATE TRACKING (avoid re-parsing images you've already processed)
# ---------------------------------------------------------
def load_processed_ids() -> set:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, OSError):
            log.warning("Could not read %s, starting with empty state.", STATE_FILE)
    return set()


def save_processed_ids(ids: set) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(ids), f)


def load_failed_images() -> dict:
    """Keyed by attachment_id (as str) -> record dict."""
    if os.path.exists(FAILED_FILE):
        try:
            with open(FAILED_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning("Could not read %s, starting with empty failure log.", FAILED_FILE)
    return {}


def save_failed_images(failed: dict) -> None:
    with open(FAILED_FILE, "w") as f:
        json.dump(failed, f, indent=2)


# ---------------------------------------------------------
# 3. VISION PARSER — jittered backoff + model fallback
# ---------------------------------------------------------
def _strip_json_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _get_retry_after_seconds(error: APIError) -> float | None:
    """Best-effort extraction of a Retry-After hint from the error, if present."""
    for attr in ("retry_after", "headers"):
        val = getattr(error, attr, None)
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, dict):
            ra = val.get("retry-after") or val.get("Retry-After")
            if ra is not None:
                try:
                    return float(ra)
                except (TypeError, ValueError):
                    pass
    return None


def _resize_for_vision(img: Image.Image) -> Image.Image:
    """
    Downscales the image if it's larger than MAX_IMAGE_DIMENSION on its
    longest side. Vision models bill image tokens based on pixel
    dimensions (tiling), so this directly cuts input token cost. Game UI
    text is large/high-contrast, so 1024px retains plenty of accuracy for
    OCR-style extraction.
    """
    if max(img.size) <= MAX_IMAGE_DIMENSION:
        return img
    resized = img.copy()
    resized.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.LANCZOS)
    return resized


async def _call_model(model_name: str, img: Image.Image) -> str:
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: client.models.generate_content(
            model=model_name,
            contents=[VISION_PROMPT, img],
        ),
    )
    return response.text


async def parse_schedule_image_with_vision(image_bytes: bytes, filename: str) -> dict | None:
    img = Image.open(io.BytesIO(image_bytes))
    img = _resize_for_vision(img)

    for model_name in MODEL_CHAIN:
        for attempt in range(1, RETRIES_PER_MODEL + 1):
            try:
                raw_text = await _call_model(model_name, img)
                cleaned = _strip_json_fences(raw_text)
                data = json.loads(cleaned)

                team_found = data.get("featured_team", "Unknown")
                rows_count = len(data.get("schedule", []))
                log.info(
                    "[SUCCESS] %s parsed via %s (%d schedule rows).",
                    team_found, model_name, rows_count,
                )
                return data

            except APIError as e:
                code = getattr(e, "code", None)
                if code == 503:
                    retry_after = _get_retry_after_seconds(e)
                    base_wait = retry_after if retry_after else 5 * (2 ** (attempt - 1))
                    wait_time = min(base_wait, MAX_BACKOFF_SECONDS)
                    wait_time += random.uniform(0, wait_time * 0.25)  # jitter
                    log.warning(
                        "[503 HIGH DEMAND] %s busy. Waiting %.1fs (attempt %d/%d on this model).",
                        model_name, wait_time, attempt, RETRIES_PER_MODEL,
                    )
                    await asyncio.sleep(wait_time)
                elif code == 429:
                    retry_after = _get_retry_after_seconds(e)
                    wait_time = retry_after if retry_after else 6 * attempt
                    log.warning("[RATE LIMIT 429] Pausing %.1fs before retry.", wait_time)
                    await asyncio.sleep(wait_time)
                elif code == 404:
                    # Model doesn't exist / isn't available to this API key --
                    # a permanent condition, not a transient one. Retrying the
                    # same model won't help; move straight to the next model.
                    log.error(
                        "[404 UNAVAILABLE] %s not accessible to this API key: %s",
                        model_name, getattr(e, "message", str(e)),
                    )
                    break
                else:
                    log.error("[API ERROR] %s: %s", code, getattr(e, "message", str(e)))
                    await asyncio.sleep(3)
            except json.JSONDecodeError as e:
                log.error("[PARSING ERROR] Bad JSON from %s for %s: %s", model_name, filename, e)
                return None
            except Exception as e:
                log.error("[UNEXPECTED ERROR] %s while processing %s: %s", type(e).__name__, filename, e)
                return None

        log.warning("[FALLBACK] %s exhausted retries for %s, trying next model...", model_name, filename)

    log.error("[FAILED] All models exhausted for %s.", filename)
    return None


# ---------------------------------------------------------
# 4. RECORD PROCESSING (unchanged logic, minor cleanup)
# ---------------------------------------------------------
def process_vision_data(data: dict) -> list[dict]:
    if not data or "featured_team" not in data or "schedule" not in data:
        return []

    featured_team = data["featured_team"].strip().title()

    # Exact match first -- this is the common case and is unambiguous. Only
    # fall back to fuzzy substring matching if nothing matches exactly, and
    # when falling back, prefer the LONGEST (most specific) candidate. The
    # naive "first substring match wins" approach previously here was
    # order-dependent (Python set iteration isn't stable) and could match
    # "Virginia Tech" or "West Virginia" to plain "Virginia" -- since
    # "virginia" is a substring of both -- silently merging two different
    # teams' schedules into one and making the other vanish from the CSV.
    if featured_team in USER_CONTROLLED_TEAMS:
        matched_team = featured_team
    else:
        candidates = [
            user_team for user_team in USER_CONTROLLED_TEAMS
            if user_team.lower() in featured_team.lower() or featured_team.lower() in user_team.lower()
        ]
        matched_team = max(candidates, key=len) if candidates else featured_team

    team_user = TEAM_TO_USER.get(matched_team, "CPU")

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    records = []
    for item in data["schedule"]:
        opponent = item.get("opponent", "BYE")
        if not opponent or opponent.upper() == "BYE":
            records.append({
                "Team": matched_team, "User": team_user,
                "Week": str(item.get("week", "")), "Date": "",
                "Location": "-", "Opponent_Rank": "-", "Opponent": "BYE",
                "Opponent_User": "-", "Matchup_Type": "BYE", "Status": "BYE",
                "Outcome": "-", "Team_Score": None, "Opponent_Score": None,
                "Margin": None, "Last_Updated": now_str,
            })
            continue

        is_user_vs_user = opponent in USER_CONTROLLED_TEAMS
        matchup_type = "User vs User" if is_user_vs_user else "User vs CPU"
        opp_user = TEAM_TO_USER.get(opponent, "CPU")
        t_score = item.get("team_score")
        o_score = item.get("opponent_score")
        outcome = item.get("outcome", "-")
        status = "Completed" if outcome in ["W", "L"] else "Upcoming"
        margin = (t_score - o_score) if (t_score is not None and o_score is not None) else None

        records.append({
            "Team": matched_team, "User": team_user,
            "Week": str(item.get("week", "")), "Date": item.get("date", ""),
            "Location": item.get("location", "Home"),
            "Opponent_Rank": str(item.get("opponent_rank", "-")),
            "Opponent": opponent, "Opponent_User": opp_user,
            "Matchup_Type": matchup_type, "Status": status, "Outcome": outcome,
            "Team_Score": t_score, "Opponent_Score": o_score, "Margin": margin,
            "Last_Updated": now_str,
        })
    return records


def get_sort_key(week_val) -> int:
    week_str = str(week_val).strip()
    if week_str.isdigit():
        return int(week_str)
    elif "conf" in week_str.lower():
        return 15
    return 99


def merge_records(df_existing, df_new: pd.DataFrame, season: int) -> pd.DataFrame:
    """
    Upserts df_new into df_existing keyed by (Season, Team, Week), and
    maintains two separate opponent-rank fields:

      - Opponent_Rank: the LIVE/current rank -- always whatever the most
        recent screenshot shows, refreshed on every run same as before.
      - Opponent_Rank_At_Game: the FROZEN rank at the moment the game was
        actually played. Once a game shows Completed for the first time,
        this locks in and is never touched again, even though the live
        column keeps moving.

    The tricky case this handles: a game is scraped as "Upcoming" in one
    run (capturing the opponent's true pre-game rank), then scraped again
    in a later run once it's "Completed" -- by which point the in-game UI
    may already be showing that opponent's rank *after* the loss, not at
    kickoff. The transition uses the earlier Upcoming snapshot's rank
    rather than trusting whatever the newer screenshot says, since that's
    the only rank value that was actually true at game time.
    """
    df_new = df_new.drop_duplicates(subset=["Team", "Week"], keep="last").copy()
    df_new["Week"] = df_new["Week"].astype(str)

    if df_existing is None or df_existing.empty:
        df_new["Opponent_Rank_At_Game"] = df_new.apply(
            lambda r: r["Opponent_Rank"] if r["Status"] == "Completed" else "", axis=1
        )
        df_new["Season"] = season
        return df_new

    df_existing = df_existing.copy()
    if "Opponent_Rank_At_Game" not in df_existing.columns:
        df_existing["Opponent_Rank_At_Game"] = ""
    df_existing["Week"] = df_existing["Week"].astype(str)

    existing_map = {
        (row["Season"], row["Team"], row["Week"]): row
        for _, row in df_existing.iterrows()
    }

    merged_rows = []
    seen_keys = set()

    for _, new_row in df_new.iterrows():
        key = (season, new_row["Team"], new_row["Week"])
        seen_keys.add(key)
        old_row = existing_map.get(key)

        merged = new_row.to_dict()
        merged["Season"] = season

        frozen_rank = ""
        if old_row is not None:
            old_frozen = old_row.get("Opponent_Rank_At_Game", "")
            if pd.notna(old_frozen) and str(old_frozen).strip() not in ("", "nan"):
                frozen_rank = old_frozen  # already locked in a prior run
            elif new_row["Status"] == "Completed":
                if old_row.get("Status") == "Upcoming":
                    frozen_rank = old_row.get("Opponent_Rank", "")  # true pre-game rank
                else:
                    frozen_rank = new_row.get("Opponent_Rank", "")  # no earlier snapshot; best available
        elif new_row["Status"] == "Completed":
            frozen_rank = new_row.get("Opponent_Rank", "")  # brand-new row, already completed

        merged["Opponent_Rank_At_Game"] = frozen_rank
        merged_rows.append(merged)

    for key, old_row in existing_map.items():
        if key not in seen_keys:
            merged_rows.append(old_row.to_dict())

    return pd.DataFrame(merged_rows)


# ---------------------------------------------------------
# 5. DISCORD BOT LISTENER — bounded concurrency
# ---------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def format_alert_message(success: bool, teams_updated: list, error_text: str = None) -> str:
    """Short status line for #bot-admin-alerts -- posted every run, success
    or failure, so a missing alert is itself a signal something's wrong
    with the automation (not just with a particular team's data)."""
    if not success:
        msg = "❌ **Schedule scraper run FAILED**"
        if error_text:
            msg += f": {error_text[:300]}"
        return msg

    if not teams_updated:
        return "✅ Schedule scraper ran successfully — no new updates found."

    teams_sorted = sorted(set(teams_updated))
    if len(teams_sorted) == 1:
        team_list = teams_sorted[0]
    elif len(teams_sorted) == 2:
        team_list = f"{teams_sorted[0]} and {teams_sorted[1]}"
    else:
        team_list = ", ".join(teams_sorted[:-1]) + f", and {teams_sorted[-1]}"
    return f"✅ Schedule scraper ran successfully — updates found for {len(teams_sorted)} team(s): {team_list}."


def format_run_breakdown(per_team_rows: dict, failed_images: dict, images_processed: int) -> str:
    """Fuller per-team breakdown -- same content that used to go to the log
    channel, now sent to #bot-admin-alerts alongside the short status line."""
    lines = [
        f"**Schedule Scraper Run** — {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"Images processed this run: {images_processed}",
        f"Teams updated: {len(per_team_rows)}",
    ]
    if per_team_rows:
        for team in sorted(per_team_rows):
            lines.append(f"  • {team}: {per_team_rows[team]} schedule row(s) written")
    else:
        lines.append("  (no new screenshots found)")

    if failed_images:
        lines.append("")
        lines.append(f"⚠️ {len(failed_images)} image(s) still failing after all models:")
        for info in list(failed_images.values())[:15]:
            filename = info.get("filename", "unknown file")
            reason = str(info.get("reason", "unknown error"))[:150]
            lines.append(f"  • {filename}: {reason}")
        if len(failed_images) > 15:
            lines.append(f"  ...and {len(failed_images) - 15} more (see {FAILED_FILE})")

    return "\n".join(lines)


async def post_alert(success: bool, teams_updated: list, per_team_rows: dict,
                      failed_images: dict, images_processed: int, error_text: str = None):
    """Posts BOTH the short status line and the fuller per-team breakdown
    to #bot-admin-alerts (SUMMARY_CHANNEL_ID)."""
    if not SUMMARY_CHANNEL_ID:
        return
    channel = bot.get_channel(SUMMARY_CHANNEL_ID)
    if not channel:
        log.warning("SUMMARY_CHANNEL_ID %s not found/accessible.", SUMMARY_CHANNEL_ID)
        return

    try:
        await channel.send(format_alert_message(success, teams_updated, error_text))
    except discord.DiscordException as e:
        log.error("Failed to post alert to #%s: %s", channel.name, e)

    breakdown_text = format_run_breakdown(per_team_rows, failed_images, images_processed)
    try:
        if len(breakdown_text) <= 1900:
            await channel.send(breakdown_text)
        else:
            buf = io.BytesIO(breakdown_text.encode("utf-8"))
            await channel.send(
                content="Run breakdown attached (too long for a single message):",
                file=discord.File(buf, filename="run_breakdown.txt"),
            )
    except discord.DiscordException as e:
        log.error("Failed to post run breakdown to #%s: %s", channel.name, e)


async def post_log_file():
    """Posts the FULL captured run log as a real file attachment to
    #bot-admin-logs (ADMIN_LOG_CHANNEL_ID) -- every run, unconditionally."""
    if not ADMIN_LOG_CHANNEL_ID:
        return
    channel = bot.get_channel(ADMIN_LOG_CHANNEL_ID)
    if not channel:
        log.warning("ADMIN_LOG_CHANNEL_ID %s not found/accessible.", ADMIN_LOG_CHANNEL_ID)
        return

    log_text = LOG_BUFFER.getvalue() or "(no log output captured)"
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    buf = io.BytesIO(log_text.encode("utf-8"))
    try:
        await channel.send(file=discord.File(buf, filename=f"scraper_run_{timestamp}.txt"))
    except discord.DiscordException as e:
        log.error("Failed to post log file to #%s: %s", channel.name, e)


async def process_one_attachment(attachment, semaphore: asyncio.Semaphore, channel_id: int, message_id: int):
    """Returns (records, failure_record_or_None)."""
    async with semaphore:
        log.info("Processing %s (id=%s)...", attachment.filename, attachment.id)
        image_bytes = await attachment.read()
        parsed_json = await parse_schedule_image_with_vision(image_bytes, attachment.filename)
        if parsed_json:
            return process_vision_data(parsed_json), None

        failure_record = {
            "attachment_id": attachment.id,
            "filename": attachment.filename,
            "channel_id": channel_id,
            "message_id": message_id,
            "last_attempt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "reason": "All models in MODEL_CHAIN exhausted retries (see log for details).",
        }
        return [], failure_record


@bot.event
async def on_ready():
    log.info("Logged in as: %s", bot.user)
    log.info("Model chain: %s", " -> ".join(MODEL_CHAIN))

    processed_ids = load_processed_ids()
    failed_images = load_failed_images()

    if "--retry-failed" in sys.argv:
        await retry_failed(processed_ids, failed_images)
        return
    run_failed = False
    error_text = None
    per_team_rows = {}
    newly_processed: list[int] = []

    try:
        # No time-window cutoff here on purpose: processed_ids (STATE_FILE) is
        # already the source of truth for "have I seen this image before," so
        # scanning full channel history is safe and cheap (Discord API calls,
        # not billed vision calls). A rolling time cutoff previously meant a
        # screenshot could silently age out of the scan window and never get
        # picked up if the script wasn't run within 24h of it being posted --
        # exactly the failure mode for an irregular/mid-season run cadence.
        all_records: list[dict] = []

        for channel_id in TARGET_SCREENSHOT_CHANNEL_ID:
            channel = bot.get_channel(channel_id)
            if not channel:
                log.warning("Channel ID %s not found/accessible, skipping.", channel_id)
                continue

            log.info("Scanning channel: #%s", channel.name)

            image_attachments = []  # list of (attachment, message_id)
            async for message in channel.history(limit=None):
                for attachment in message.attachments:
                    if attachment.content_type and attachment.content_type.startswith("image/"):
                        if attachment.id in processed_ids:
                            continue  # already parsed in a prior run
                        image_attachments.append((attachment, message.id))

            log.info("Found %d new schedule image(s) to process.", len(image_attachments))

            semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
            tasks = [
                process_one_attachment(a, semaphore, channel_id, msg_id)
                for a, msg_id in image_attachments
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for (attachment, msg_id), result in zip(image_attachments, results):
                if isinstance(result, Exception):
                    log.error("Attachment %s failed with %s: %s", attachment.filename, type(result).__name__, result)
                    failed_images[str(attachment.id)] = {
                        "attachment_id": attachment.id,
                        "filename": attachment.filename,
                        "channel_id": channel_id,
                        "message_id": msg_id,
                        "last_attempt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "reason": f"Unexpected exception: {type(result).__name__}: {result}",
                    }
                    continue

                records, failure_record = result
                if records:
                    all_records.extend(records)
                    newly_processed.append(attachment.id)
                    failed_images.pop(str(attachment.id), None)  # succeeded on retry, clear old failure if any
                elif failure_record:
                    log.error(
                        "[LOGGED FAILURE] %s could not be parsed by any model — saved to %s for later retry.",
                        attachment.filename, FAILED_FILE,
                    )
                    failed_images[str(attachment.id)] = failure_record

        if all_records:
            df_new = pd.DataFrame(all_records)
            df_existing = pd.read_csv(CSV_FILE) if os.path.exists(CSV_FILE) else None
            df_combined = merge_records(df_existing, df_new, SEASON)

            df_combined["Week_Sort"] = df_combined["Week"].apply(get_sort_key)
            df_combined = df_combined.sort_values(by=["Team", "Week_Sort"]).drop(columns=["Week_Sort"])
            df_combined.to_csv(CSV_FILE, index=False)
            log.info("COMPLETED: Saved %d total records to %s.", len(df_combined), CSV_FILE)
        else:
            log.info("No new schedule entries were parsed this run.")

        if newly_processed:
            processed_ids.update(newly_processed)
            save_processed_ids(processed_ids)
            log.info("Marked %d image(s) as processed (won't be re-scanned next run).", len(newly_processed))

        save_failed_images(failed_images)
        if failed_images:
            log.warning(
                "%d image(s) still failing after all models — run with --retry-failed to retry them. See %s.",
                len(failed_images), FAILED_FILE,
            )

        per_team_rows = {}
        for r in all_records:
            per_team_rows[r["Team"]] = per_team_rows.get(r["Team"], 0) + 1
    except Exception as e:
        run_failed = True
        error_text = f"{type(e).__name__}: {e}"
        log.exception("Unhandled error during scraper run:")

    if run_failed:
        await post_alert(False, [], per_team_rows, failed_images, len(newly_processed), error_text=error_text)
    else:
        await post_alert(True, list(per_team_rows.keys()), per_team_rows, failed_images, len(newly_processed))
    await post_log_file()

    log.info("Notifications sent. Closing Discord connection now...")
    await bot.close()
    log.info("Discord connection closed cleanly.")

    if run_failed:
        sys.exit(1)


async def retry_failed(processed_ids: set, failed_images: dict) -> None:
    """Re-fetch and re-attempt every image in failed_images.json (fresh Discord read, so
    expired CDN URLs aren't an issue — we look the message back up via channel_id/message_id)."""
    if not failed_images:
        log.info("No failed images on record (%s is empty). Nothing to retry.", FAILED_FILE)
        await bot.close()
        return

    log.info("Retrying %d previously-failed image(s)...", len(failed_images))
    all_records: list[dict] = []
    newly_processed: list[int] = []
    still_failed: dict = {}
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def retry_one(attachment_id_str: str, record: dict):
        async with semaphore:
            channel = bot.get_channel(record["channel_id"])
            if not channel:
                log.error("Channel %s no longer accessible for %s, skipping.", record["channel_id"], record["filename"])
                return record
            try:
                message = await channel.fetch_message(record["message_id"])
            except discord.NotFound:
                log.error("Original message for %s was deleted, dropping from retry queue.", record["filename"])
                return None  # give up permanently — nothing left to retry
            except discord.HTTPException as e:
                log.error("Could not fetch message for %s: %s", record["filename"], e)
                return record

            attachment = discord.utils.get(message.attachments, id=record["attachment_id"])
            if not attachment:
                log.error("Attachment %s no longer on message, dropping from retry queue.", record["filename"])
                return None

            image_bytes = await attachment.read()
            parsed_json = await parse_schedule_image_with_vision(image_bytes, attachment.filename)
            if parsed_json:
                recs = process_vision_data(parsed_json)
                all_records.extend(recs)
                newly_processed.append(attachment.id)
                log.info("[RETRY SUCCESS] %s parsed on retry.", attachment.filename)
                return None  # cleared
            record["last_attempt"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return record

    tasks = [retry_one(aid, rec) for aid, rec in failed_images.items()]
    results = await asyncio.gather(*tasks)
    for aid, result in zip(failed_images.keys(), results):
        if result is not None:
            still_failed[aid] = result

    if all_records:
        df_new = pd.DataFrame(all_records)
        df_existing = pd.read_csv(CSV_FILE) if os.path.exists(CSV_FILE) else None
        df_combined = merge_records(df_existing, df_new, SEASON)

        df_combined["Week_Sort"] = df_combined["Week"].apply(get_sort_key)
        df_combined = df_combined.sort_values(by=["Team", "Week_Sort"]).drop(columns=["Week_Sort"])
        df_combined.to_csv(CSV_FILE, index=False)
        log.info("Saved %d record(s) recovered from retry.", len(df_new))

    if newly_processed:
        processed_ids.update(newly_processed)
        save_processed_ids(processed_ids)

    save_failed_images(still_failed)
    log.info(
        "Retry complete: %d recovered, %d still failing.",
        len(newly_processed), len(still_failed),
    )
    await bot.close()


if __name__ == "__main__":
    # NOTE: the CSV/state files are no longer wiped on every startup -- the
    # whole point of the state file is incremental runs. Delete a season's
    # CSV_FILE and STATE_FILE manually if you ever want a clean rebuild of
    # just that season from scratch.
    #
    # Usage:
    #   python schedule_scraper_genai.py                    -> prompts for season, scans everything
    #   python schedule_scraper_genai.py --season 2026       -> skip the prompt (e.g. scheduled runs)
    #   python schedule_scraper_genai.py --retry-failed       -> retries that season's failed_images_<season>.json
    #                                                            (still prompts/needs --season, since retry state is per-season)
    SEASON = resolve_season()
    CSV_FILE = f"dynasty_data_{SEASON}.csv"
    STATE_FILE = f"processed_images_{SEASON}.json"
    FAILED_FILE = f"failed_images_{SEASON}.json"

    USER_TEAMS = resolve_user_teams(SEASON)
    TEAM_TO_USER = {team: user for user, team in USER_TEAMS.items()}
    USER_CONTROLLED_TEAMS = set(USER_TEAMS.values())

    if os.path.exists(CSV_FILE):
        log.info("Season %d -> %s already exists; this run will update it in place.", SEASON, CSV_FILE)
    else:
        log.info("Season %d -> %s doesn't exist yet; this run will create it fresh.", SEASON, CSV_FILE)

    bot.run(BOT_TOKEN)

    # bot.run() blocks until the bot fully disconnects, but discord.py's own
    # cleanup (closing the underlying aiohttp session, etc.) has been known
    # to leave the process technically alive even after all real work --
    # including on_ready()'s own bot.close() call -- has finished. That's
    # invisible locally (you'd just see the terminal prompt come back a
    # beat late), but in GitHub Actions it means the step never registers
    # as complete and just hangs indefinitely. Force a hard exit here so
    # the step always ends the moment bot.run() actually returns.
    log.info("Scraper finished — exiting.")
    sys.exit(0)