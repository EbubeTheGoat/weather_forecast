import os
import requests
from dotenv import load_dotenv
from openai import OpenAI

from api.logger_config import get_logger
from api.database import SessionLocal
from api.model import User

load_dotenv()
logger = get_logger("worker")

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


# -----------------------------
# SIMPLE IN-MEMORY CACHE (upgrade to Redis later)
# -----------------------------
location_cache = {}


# -----------------------------
# HELPERS
# -----------------------------
def clean_place(value: str) -> str:
    if not value:
        return ""
    return value.strip().replace("State", "").strip()


# -----------------------------
# PRIMARY GEOCODER (OPEN-METEO)
# -----------------------------
def geocode_open_meteo(query: str):
    url = "https://geocoding-api.open-meteo.com/v1/search"

    params = {"name": query, "count": 1, "format": "json"}

    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()

    logger.info(f"Open-Meteo geocode '{query}': {data}")

    if data.get("results"):
        result = data["results"][0]
        return {
            "latitude": result["latitude"],
            "longitude": result["longitude"],
            "name": result.get("name", "Unknown"),
            "state": result.get("admin1", ""),
            "country": result.get("country", "Unknown"),
        }

    return None


# -----------------------------
# FALLBACK GEOCODER (OPENSTREETMAP NOMINATIM)
# -----------------------------
def geocode_nominatim(city, state, country):
    url = "https://nominatim.openstreetmap.org/search"

    query = f"{city}, {state}, {country}".strip(", ")

    params = {
        "q": query,
        "format": "json",
        "limit": 1
    }

    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()

    logger.info(f"Nominatim geocode '{query}': {data}")

    if data:
        return {
            "latitude": float(data[0]["lat"]),
            "longitude": float(data[0]["lon"]),
            "name": data[0].get("display_name", "Unknown"),
            "state": state,
            "country": country,
        }

    return None


# -----------------------------
# MAIN GEOCODING WRAPPER
# -----------------------------
def get_coordinates(city, state, country):
    city = clean_place(city)
    state = clean_place(state)
    country = clean_place(country)

    cache_key = f"{city}-{state}-{country}"
    if cache_key in location_cache:
        return location_cache[cache_key]

    # IMPORTANT: order matters (Nigeria-friendly strategy)
    queries = [
        f"{city}",
        f"{city}, {country}",
        f"{city}, {state}",
        f"{city}, {state}, {country}",
    ]

    # Fallback to Nominatim
    try:
        result = geocode_nominatim(city, state, country)
        if result:
            location_cache[cache_key] = result
            return result
    except Exception as e:
        logger.error(f"Nominatim failed: {e}")

    logger.error(f"Geocoding failed completely: {city}, {state}, {country}")
    raise ValueError(f"Could not find coordinates for {city}, {state}, {country}")


# -----------------------------
# WEATHER FETCH
# -----------------------------
def get_weather_forecast(lat: float, lon: float):
    url = "https://api.open-meteo.com/v1/forecast"

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation",
        "current_weather": True,
        "timezone": "auto",
        "forecast_days": 1
    }

    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


# -----------------------------
# AI WEATHER INTERPRETER
# -----------------------------
def predict_rain(weather_data: dict):
    prompt = f"""
You are an expert meteorologist.

Rules:
- ONLY use provided data
- DO NOT hallucinate
- Use West African Time context
- Be concise
- Format in Markdown

Tasks:
1. Is today rainy or sunny (probability-based)?
2. Peak weather hours
3. Short summary

DATA:
{weather_data}
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"AI prediction error: {e}")
        return "Weather data unavailable."


# -----------------------------
# TELEGRAM SENDER
# -----------------------------
def send_telegram_message(chat_id: str, message: str):
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("Missing Telegram token")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


# -----------------------------
# MAIN JOB
# -----------------------------
def job_fetch_and_send_forecast():
    with SessionLocal() as db:
        try:
            users = db.query(User).filter(
                User.city != None,
                User.current_step == "CONFIRMED"
            ).all()

            for user in users:
                try:
                    logger.info(
                        f"User {user.phone_number} → "
                        f"{user.city}, {user.state}, {user.country}"
                    )

                    loc = get_coordinates(user.city, user.state, user.country)
                    weather = get_weather_forecast(loc["latitude"], loc["longitude"])
                    prediction = predict_rain(weather)

                    message = (
                        f"🌍 Weather Forecast for {loc['name']}, {loc['country']}\n\n"
                        f"{prediction}"
                    )

                    send_telegram_message(user.phone_number, message)

                except Exception as e:
                    logger.error(f"User failed {user.phone_number}: {e}")

                    send_telegram_message(
                        user.phone_number,
                        "⚠️ Could not fetch your forecast. Reply 'change' to update location."
                    )

        except Exception as e:
            logger.error(f"Cron job failed: {e}")
