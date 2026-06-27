import os
import requests
from dotenv import load_dotenv
from openai import OpenAI
from api.logger_config import get_logger
from api.database import SessionLocal
from api.model import User

load_dotenv()
logger = get_logger("worker")

client = OpenAI(
    api_key=os.environ.get("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1" 
)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

def get_coordinates(city, state, country):
    base_url = "https://geocoding-api.open-meteo.com/v1/search"
    queries = [
        f"{city}, {state}, {country}",
        f"{city}, {country}",
        f"{state}, {country}",
    ]

    for query in queries:
        try:
            params = {"name": query, "count": 1, "format": "json"}
            response = requests.get(base_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            if "results" in data and data["results"]:
                result = data["results"][0]
                return {
                    "latitude": result["latitude"],
                    "longitude": result["longitude"],
                    "name": result.get("name", "Unknown"),
                    "state": result.get("admin1", ""),
                    "country": result.get("country", "Unknown"),
                }
        except requests.RequestException as e:
            logger.error(f"Network error for '{query}': {e}")
            continue

    # Log exactly what's in the DB so you can see what's failing
    logger.error(f"Could not resolve coordinates — city='{city}' state='{state}' country='{country}'")
    raise ValueError(f"Could not find coordinates for {city}, {state}, {country}")

def get_weather_forecast(lat: float, lon: float):
    base_url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation",
        "current_weather": True,
        "timezone": "auto",
        "forecast_days": 1 
    }
    response = requests.get(base_url, params=params, timeout=5)
    response.raise_for_status()
    return response.json()

def predict_rain(weather_data: dict):
    prompt = f"""
    You are an expert meteorologist. Analyze this raw weather data:
    1. Is rain > 50% likely?
    2. Peak precipitation hour?
    3. Max temperature?
    4. If it'll rain,be specific about the hour(s)
    5. Use ONLY the data provided.
    6. Do NOT infer missing values.
    7.If something is not present, say "unknown".
    8.Do not add external knowledge.
    9. Give the information using west african time
    Keep it concise.
    Data: {weather_data}
    """
    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-20b", # Ensure this model ID exists in Groq's current docs
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Error predicting rain: {e}")
        return "Weather data unavailable at the moment."

def send_telegram_message(chat_id: str, message: str):
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("Telegram bot token not set. Skipping.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, json=payload, timeout=5)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return False

def job_fetch_and_send_forecast():
    with SessionLocal() as db:
        try:
            users = db.query(User).filter(
                User.city != None,
                User.current_step == "CONFIRMED"  # ← only fully onboarded users
            ).all()

            for user in users:
                try:
                    # Log what's in the DB so you can verify
                    logger.info(f"Processing user {user.phone_number} — city={user.city}, state={user.state}, country={user.country}")

                    logger.info("STEP 1: geocoding")
                    scraped = get_coordinates(user.city, user.state, user.country)
                    logger.info("STEP 2: weather fetch")
                    weather_news = get_weather_forecast(scraped["latitude"], scraped["longitude"])
                    logger.info("STEP 3: AI prediction")
                    prediction = predict_rain(weather_news)

                    message = f"Weather Forecast for {scraped['name']}, {scraped['country']}:\n\n{prediction}"
                    logger.info("STEP 4: sending telegram")
                    send_telegram_message(user.phone_number, message)

                except Exception as e:
                    logger.exception("PIPELINE FAILED")
                    send_telegram_message(user.phone_number, "⚠️ Could not fetch your forecast. Try updating your location by replying 'change'.")
                    continue

        except Exception as e:
            logger.error(f"Error in cron job execution: {e}")
