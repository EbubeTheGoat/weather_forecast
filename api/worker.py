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
        "Lagos, Nigeria"
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

    raise ValueError(f"Could not find coordinates for {city} or {state}")

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
    You are an expert meteorologist. Analyze this raw weather data and tell me:
    1. If it would be a sunny or rainy day?
    2. If it's going to rain, when will it start and end?
    3. How heavy will it be if it rains?
    4. Provide a brief summary of the weather conditions for the day.
    Keep it concise.
    Data: {weather_data}
    """
    try:
        response = client.chat.completions.create(
            model="llama3-8b-8192", # Ensure this model ID exists in Groq's current docs
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
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload, timeout=5)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to send Telegram message: {e}")
        return False

def job_fetch_and_send_forecast():
    # Context manager properly closes the session to prevent leaks
    with SessionLocal() as db:
        try:
            # We must iterate over actual users, not the class blueprint
            users = db.query(User).filter(User.city != None).all()
            for user in users:
                try:
                    scraped = get_coordinates(user.city, user.state, user.country)
                    weather_news = get_weather_forecast(scraped["latitude"], scraped["longitude"])
                    prediction = predict_rain(weather_news)

                    message = f"Weather Forecast for <b>{scraped['name']}, {scraped['country']}</b>:\n\n{prediction}"
                    
                    success = send_telegram_message(user.phone_number, message)
                    if not success:
                        send_telegram_message(user.phone_number, "Failed to send complete forecast. Will do better next time!")
                
                except Exception as e:
                    logger.error(f"Error processing user {user.phone_number}: {e}")
                    continue
        except Exception as e:
            logger.error(f"Error in cron job execution: {e}")