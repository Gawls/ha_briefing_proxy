from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from openai import AsyncOpenAI, RateLimitError, APIError
from typing import Any, List, Dict, Optional
from pathlib import Path
import os
import httpx
import json
import re
import asyncio
import logging
import tempfile

VERSION = "1.1.0"
logger = logging.getLogger("uvicorn.error")

# Required
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
HA_WEBHOOK_URL = os.environ.get("HA_WEBHOOK_URL", "").strip()
SEND_WEBHOOK = os.environ.get("SEND_WEBHOOK", "false").strip().lower() == "true"

MODEL_NAME = (
    os.environ.get("MODEL_NAME")
    or os.environ.get("OPENAI_MODEL")
    or "gpt-5-mini"
)

NHL_TEAM = os.environ.get("NHL_TEAM", "EDM")
NHL_SCOREBOARD_TEAM_URL = "https://api-web.nhle.com/v1/scoreboard/{team}/now"

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
LATEST_PATH = DATA_DIR / "latest_briefing.json"  # /data persists for HA add-ons
LATEST_PATH.parent.mkdir(parents=True, exist_ok=True)

# Preserve the existing ending history across add-on updates.
ENDINGS_PATH = DATA_DIR / "briefing_endings.json"
ENDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
MAX_ENDINGS = int(os.environ.get("MAX_ENDINGS", "60"))

app = FastAPI(title="HA Briefing Proxy", version=VERSION)
client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=40.0, max_retries=0) if OPENAI_API_KEY else None


class BriefingRequest(BaseModel):
    weather: str = ""
    events: Any = Field(default_factory=dict)


@app.get("/health")
async def health():
    return {
        "ok": True,
        "version": VERSION,
        "model": MODEL_NAME,
        "has_key": bool(OPENAI_API_KEY),
        "cached": bool(load_latest()["text"]),
        "audio_generation": "home_assistant",
        "send_webhook": SEND_WEBHOOK,
    }


@app.get("/debug/env")
async def debug_env():
    return {"has_key": bool(OPENAI_API_KEY), "model": MODEL_NAME}


def atomic_write_json(path: Path, payload: Any) -> None:
    """Publish complete JSON so readers never see a partially written briefing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(payload, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def save_latest(text: str, generation_mode: str, warning: Optional[str]) -> Dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("Cannot save an empty briefing.")
    payload = {
        "text": text,
        "audio_url": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "character_count": len(text),
        "generation_mode": generation_mode,
        "warning": warning,
    }
    atomic_write_json(LATEST_PATH, payload)
    return payload


def load_latest() -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    try:
        loaded = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        pass
    text = data.get("text") if isinstance(data.get("text"), str) else ""
    return {
        "text": text,
        # Old stored MP3 URLs must not be advertised after the migration.
        "audio_url": None,
        "updated_at": data.get("updated_at"),
        "character_count": len(text),
        "generation_mode": data.get("generation_mode", "unknown"),
        "warning": data.get("warning"),
    }


async def post_to_ha(payload: Dict[str, Any]) -> None:
    """Optional legacy notification, after the direct response has been sent."""
    if not SEND_WEBHOOK or not HA_WEBHOOK_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.post(HA_WEBHOOK_URL, json=payload)
            r.raise_for_status()
    except Exception as exc:
        # The URL can contain a webhook secret; do not log the exception text.
        logger.warning("Briefing webhook delivery failed (%s).", type(exc).__name__)


# -----------------------
# NEW: Ending memory
# -----------------------
def load_recent_endings() -> List[str]:
    if not ENDINGS_PATH.exists():
        return []
    try:
        data = json.loads(ENDINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
        return []
    except Exception:
        return []


def save_recent_ending(ending: str) -> None:
    ending = (ending or "").strip()
    if not ending:
        return

    recent = load_recent_endings()

    # de-dup exact matches (case-insensitive)
    recent_lower = [e.lower() for e in recent]
    if ending.lower() in recent_lower:
        # move it to the end anyway (keeps list tidy)
        recent = [e for e in recent if e.lower() != ending.lower()]

    recent.append(ending)
    recent = recent[-MAX_ENDINGS:]
    atomic_write_json(ENDINGS_PATH, recent)


def recent_endings_block() -> str:
    recent = load_recent_endings()
    if not recent:
        return "(none yet)"
    # bullet list helps the model avoid variants
    return "\n".join(f"- {e}" for e in recent)


def parse_two_line_output(model_text: str) -> tuple[str, str]:
    """
    Expects:
      LINE 1: briefing paragraph
      LINE 2: FUN_ENDING: ...
    Returns (briefing, ending). If missing ending, ending="".
    """
    if not model_text:
        return "", ""

    lines = [ln.strip() for ln in model_text.splitlines() if ln.strip()]

    # Find FUN_ENDING line anywhere
    ending = ""
    for ln in lines:
        if ln.upper().startswith("FUN_ENDING:"):
            ending = ln.split(":", 1)[1].strip()
            break

    # Briefing is everything except FUN_ENDING lines, joined into one paragraph
    briefing_parts = [ln for ln in lines if not ln.upper().startswith("FUN_ENDING:")]
    briefing = " ".join(briefing_parts).strip()

    return briefing, ending


# -----------------------
# Existing helpers (yours)
# -----------------------
def extract_weather_alerts(forecast: str) -> List[Dict[str, Any]]:
    if not forecast:
        return []
    text = forecast.lower()
    alerts: List[Dict[str, Any]] = []
    if "freezing rain" in text or "ice pellets" in text:
        alerts.append({"type": "freezing_precip", "severity": "high",
                      "message": "Risk of freezing rain or ice pellets — roads and sidewalks may be icy."})

    snow_amounts = re.findall(r"(\d+)\s*cm", text)
    if snow_amounts:
        max_cm = max(int(x) for x in snow_amounts)
        if max_cm >= 10:
            severity, msg = "high", f"Heavy snow, around {max_cm} cm."
        elif max_cm >= 5:
            severity, msg = "medium", f"Notable snow, around {max_cm} cm."
        else:
            severity, msg = "low", f"Light snow, around {max_cm} cm."
        alerts.append({"type": "snow", "severity": severity, "amount_cm": max_cm, "message": msg})

    gust_match = re.search(r"gust(?:ing)?\s+to\s+(\d+)\s*km/h", text)
    if gust_match:
        gust = int(gust_match.group(1))
        severity = "high" if gust >= 70 else "medium" if gust >= 50 else "low"
        alerts.append({"type": "wind", "severity": severity, "gust_kmh": gust, "message": f"Wind gusts up to {gust} km/h."})

    wc_match = re.search(r"wind chill(?: near)?\s*(-?\d+)", text)
    if wc_match:
        wind_chill = int(wc_match.group(1))
        if wind_chill <= -30:
            severity, msg = "high", f"Brutal wind chill of {wind_chill}. Exposed skin can freeze quickly."
        elif wind_chill <= -20:
            severity, msg = "medium", f"Very cold wind chill of {wind_chill}. Dress in warm layers."
        else:
            severity, msg = "low", f"Chilly wind chill of {wind_chill}."
        alerts.append({"type": "wind_chill", "severity": severity, "wind_chill": wind_chill, "message": msg})

    if "warning" in text or "advisory" in text or "special weather statement" in text:
        alerts.append({"type": "statement", "severity": "check",
                      "message": "Environment Canada has issued a weather statement or warning."})

    severity_rank = {"high": 0, "medium": 1, "low": 2, "check": 3}
    alerts.sort(key=lambda a: severity_rank.get(a.get("severity", "low"), 3))
    return alerts


async def fetch_oilers_recap() -> Optional[str]:
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.get(NHL_SCOREBOARD_TEAM_URL.format(team=NHL_TEAM), timeout=5)
            resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    games: List[Dict[str, Any]] = []
    for day in data.get("gamesByDate", []):
        for game in day.get("games", []):
            if game.get("gameState") in ("OFF", "FINAL"):
                games.append(game)
    if not games:
        return None

    games.sort(key=lambda g: (g.get("gameDate") or "", g.get("startTimeUTC") or ""), reverse=True)
    last = games[0]

    game_date_str = last.get("gameDate")
    try:
        game_date = datetime.strptime(game_date_str, "%Y-%m-%d").date() if game_date_str else None
    except Exception:
        game_date = None

    if game_date is not None:
        days_ago = (datetime.utcnow().date() - game_date).days
        if days_ago > 1:
            return None
    else:
        days_ago = None

    home = last.get("homeTeam", {}) or {}
    away = last.get("awayTeam", {}) or {}

    home_abbrev = home.get("abbrev")
    away_abbrev = away.get("abbrev")
    home_score = home.get("score")
    away_score = away.get("score")
    if None in (home_abbrev, away_abbrev, home_score, away_score):
        return None

    if home_abbrev == NHL_TEAM:
        edm_score, opp_score = home_score, away_score
        opp_name = away.get("commonName", {}).get("default") or away.get("name", {}).get("default") or away_abbrev
        location_phrase = "at home"
    elif away_abbrev == NHL_TEAM:
        edm_score, opp_score = away_score, home_score
        opp_name = home.get("commonName", {}).get("default") or home.get("name", {}).get("default") or home_abbrev
        location_phrase = "on the road"
    else:
        return None

    if edm_score > opp_score:
        recap_core = f"the Oilers beat the {opp_name} {edm_score}-{opp_score}"
    elif edm_score < opp_score:
        higher, lower = max(edm_score, opp_score), min(edm_score, opp_score)
        recap_core = f"the Oilers lost {higher}-{lower} to the {opp_name}"
    else:
        recap_core = f"the Oilers tied the {opp_name} {edm_score}-{opp_score}"

    pt = (last.get("periodDescriptor", {}) or {}).get("periodType")
    extra = " in overtime" if pt == "OT" else " in a shootout" if pt == "SO" else ""
    qualifier = "Earlier today, " if days_ago == 0 else "Last night, " if days_ago == 1 else "In their most recent game, "
    return f"{qualifier}{recap_core}{extra} {location_phrase}."


@app.get("/briefing_audio")
async def briefing_audio():
    return JSONResponse(
        {"detail": "Audio is now generated by Home Assistant's configured TTS integration."},
        status_code=410,
    )


@app.get("/briefing/latest")
async def briefing_latest():
    return JSONResponse(load_latest())


def extract_calendar_note(events_obj: Any) -> str:
    if not isinstance(events_obj, dict):
        return ""
    meta = events_obj.get("meta") or {}
    if isinstance(meta, dict):
        note = meta.get("calendar_note")
        if isinstance(note, str) and note.strip():
            return note.strip()

        failed = meta.get("calendar_failed_list")
        if isinstance(failed, list) and failed:
            failed_clean = [str(x) for x in failed if x]
            if failed_clean:
                return f"Quick note: calendar access is unavailable for {', '.join(failed_clean)} right now, so events may be missing."

        ok_all = meta.get("calendar_ok_all")
        if ok_all is False:
            return "Quick note: calendar access is temporarily unavailable, so events may be missing."
    return ""


@app.post("/briefing")
async def create_briefing(req: BriefingRequest, background_tasks: BackgroundTasks):
    if client is None:
        raise HTTPException(status_code=503, detail="The OpenAI text-generation key is not configured.")

    # Normalize events into a dict
    try:
        if isinstance(req.events, dict):
            events_obj = req.events
        elif isinstance(req.events, str):
            try:
                events_obj = json.loads(req.events)
            except Exception:
                events_obj = {"raw": req.events}
        else:
            events_obj = {"raw": req.events}
    except Exception:
        events_obj = {"raw": req.events}

    calendar_note = extract_calendar_note(events_obj)
    events_pretty = json.dumps(events_obj, indent=2, default=str)

    weather_alerts = extract_weather_alerts(req.weather or "")
    alerts_pretty = json.dumps(weather_alerts, indent=2)

    oilers_recap_text = (await fetch_oilers_recap()) or ""

    # NEW: recent endings blacklist
    recent_endings_text = recent_endings_block()

    system_msg = (
        "You write friendly morning briefings for the Gawley family that lives in St. Albert, Alberta. "
        "Tone: warm, casual, slightly playful, and easy to follow when spoken aloud. "
        "You receive weather, calendar JSON, parsed weather alerts, an optional Oilers recap, and possibly a calendar status note. "
        "If the calendar status note is present (non-empty), mention it once near the beginning in a natural way. "
        "Never invent alerts or events. Avoid repeating any recent fun ending provided. "
        "Output format MUST be exactly two lines:\n"
        "LINE 1: the full briefing as one continuous paragraph (no jokes/rap here)\n"
        "LINE 2: FUN_ENDING: <one brand-new mom joke OR dad joke OR short 2-line rap>\n"
        "Rules for FUN_ENDING: must be original, not a common stock joke, no repeats, no close variants, family-friendly. "
        "If calendar access is unavailable for some calendars, still mention events explicitly supplied from available calendars. "
        "Do not invent events for unavailable calendars, and do not claim there are no events while any calendar is unavailable."
    )

    user_msg = (
        "Weather summary:\n"
        f"{req.weather}\n\n"
        "Parsed weather alerts (JSON):\n"
        f"{alerts_pretty}\n\n"
        f"Calendar status note (may be empty):\n{calendar_note}\n\n"
        "Calendar events (JSON):\n"
        f"{events_pretty}\n\n"
        "Oilers recap (may be empty):\n"
        f"{oilers_recap_text}\n\n"
        "Recently used fun endings (avoid repeating these or close variants):\n"
        f"{recent_endings_text}\n"
    )

    generation_mode = "ai"
    warning: Optional[str] = None

    try:
        response = await asyncio.wait_for(client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
        ), timeout=45.0)

        raw = (response.choices[0].message.content or "").strip()
        briefing, ending = parse_two_line_output(raw)

        # Plain paragraph responses are valid; a FUN_ENDING alone is not a briefing.
        if not briefing and not ending:
            briefing = raw
        if not briefing:
            raise ValueError("The text model returned no briefing paragraph.")

        # Final text spoken/sent to HA includes ending appended
        text = f"{briefing} {ending}".strip()
        if not text:
            raise ValueError("The text model returned an empty briefing.")

        # A history write error should not discard a successfully generated briefing.
        if ending:
            try:
                save_recent_ending(ending)
            except OSError:
                logger.warning("Could not update the recent-ending history.")

    except RateLimitError:
        generation_mode, warning = "fallback", "rate_limit"
        text = f"{calendar_note} Good morning. I hit a rate limit. Here’s the simple version. {req.weather}".strip()
    except APIError:
        generation_mode, warning = "fallback", "api_error"
        text = f"{calendar_note} Good morning. The AI service had an error. Here’s the simple version. {req.weather}".strip()
    except asyncio.TimeoutError:
        generation_mode, warning = "fallback", "timeout"
        text = f"{calendar_note} Good morning. The AI service took too long. Here’s the simple version. {req.weather}".strip()
    except Exception as exc:
        generation_mode, warning = "fallback", "generation_error"
        logger.warning("Briefing text generation failed (%s).", type(exc).__name__)
        text = f"{calendar_note} Good morning. Something went wrong. Here’s the simple version. {req.weather}".strip()

    if warning:
        logger.warning("Returning a fresh weather fallback: %s.", warning)
    try:
        latest = save_latest(text, generation_mode, warning)
    except OSError as exc:
        logger.error("Could not save the latest briefing (%s).", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Could not save the latest briefing.") from exc

    if SEND_WEBHOOK and HA_WEBHOOK_URL:
        background_tasks.add_task(post_to_ha, latest)

    return {"status": "ok", **latest}
