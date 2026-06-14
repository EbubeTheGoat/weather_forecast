import os
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from sqlalchemy.orm import Session

from api.database import engine, Base, get_db
import api.model
from api.schemas import UserBase
from api.logger_config import get_logger
from api.worker import job_fetch_and_send_forecast, send_telegram_message

# Initialize DB Models
Base.metadata.create_all(bind=engine)
logger = get_logger("main")

LIMITER = Limiter(key_func=get_remote_address, default_limits=["1000/hour"])

# Simple in-memory cache for state management
STATE_CACHE = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application starting up...")
    yield
    logger.info("Application shutting down...")

app = FastAPI(lifespan=lifespan)
app.state.limiter = LIMITER
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def normalize_name(name: str) -> str:
    cleaned = (name or "").strip()
    if len(cleaned) < 2:
        raise HTTPException(status_code=400, detail="Please enter your full name.")
    return cleaned

@app.get("/api/cron")
def trigger_cron_job(request: Request):
    auth_header = request.headers.get("Authorization")
    expected_token = f"Bearer {os.getenv('CRON_SECRET')}"
    if auth_header != expected_token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        job_fetch_and_send_forecast()
        return {"status": "Cron job executed successfully"}
    except Exception as e:
        logger.error(f"Error executing cron job: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

@app.post("/register_lead")
def register_lead(lead: UserBase, db: Session = Depends(get_db)):
    name = normalize_name(lead.name)
    try:
        db_lead = db.query(api.model.RegistrationLead).filter(api.model.RegistrationLead.name == name).first()
        if not db_lead:
            db_lead = api.model.RegistrationLead(name=name)
            db.add(db_lead)
            db.commit()
            db.refresh(db_lead)
        return {"id": db_lead.id, "name": db_lead.name}
    except Exception as e:
        logger.error(f"Error registering lead: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

@app.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    """Handles incoming JSON data from Telegram."""
    data = await request.json()
    
    if "message" not in data or "text" not in data["message"]:
        return {"status": "ignored"}

    chat_id = str(data["message"]["chat"]["id"])
    incoming = data["message"]["text"].strip()

    user = db.query(api.model.User).filter(api.model.User.phone_number == chat_id).first()

    if not user:
        user = api.model.User(phone_number=chat_id, current_step="IDLE")
        db.add(user)
        db.commit()

    # Prefer cache, fallback to database state
    state = STATE_CACHE.get(chat_id, user.current_step)

    # State Machine Logic
    if state == "IDLE":
        msg = "🚀 <b>Welcome to Weather forecast!</b>\n\nWhat city do you want the forecast for?"
        if send_telegram_message(chat_id, msg):
            user.current_step = "AWAITING_CITY"
            db.commit()
            STATE_CACHE[chat_id] = "AWAITING_CITY"
        return {"status": "ok"}

    if state == "AWAITING_CITY":
        if len(incoming) < 2 or incoming.lower() == "/start":
            send_telegram_message(chat_id, "Please enter a valid city name.")
            return {"status": "ok"}

        user.city = incoming # Assuming they input a city for weather tracking
        user.current_step = "CONFIRMED_CITY"
        db.commit()
    if state == "CONFIRMED_CITY":
        msg = "🚀 <b>Welcome to Weather forecast!</b>\n\nWhat state do you reside in ?"
        if send_telegram_message(chat_id, msg):
            user.current_step = "AWAITING_STATE"
            db.commit()
            STATE_CACHE[chat_id] = "AWAITING_STATE"
        return {"status": "ok"}

    if state == "AWAITING_STATE":
        if len(incoming) < 2 or incoming.lower() == "/start":
            send_telegram_message(chat_id, "Please enter a valid state name.")
            return {"status": "ok"}

        user.state = incoming # Assuming they input a city for weather tracking
        user.current_step = "CONFIRMED_STATE"
        db.commit()
    if state == "CONFIRMED_STATE":
        msg = "🚀 <b>Welcome to Weather forecast!</b>\n\nWhat country do you reside in ?"
        if send_telegram_message(chat_id, msg):
            user.current_step = "AWAITING_COUNTRY"
            db.commit()
            STATE_CACHE[chat_id] = "AWAITING_COUNTRY"
        return {"status": "ok"}

    if state == "AWAITING_COUNTRY":
        if len(incoming) < 2 or incoming.lower() == "/start":
            send_telegram_message(chat_id, "Please enter a valid country name.")
            return {"status": "ok"}

        user.country = incoming # Assuming they input a city for weather tracking
        user.current_step = "CONFIRMED_COUNTRY"
        db.commit()
        STATE_CACHE[chat_id] = "CONFIRMED"

        confirm_msg = f"✅ Got it! You'll receive weather updates for <b>{incoming}</b>.\n\nReply 'change' anytime to update your location."
        send_telegram_message(chat_id, confirm_msg)
        return {"status": "ok"}

    if state == "CONFIRMED":
        if incoming.lower() == "change":
            user.current_step = "AWAITING_LOCATION"
            db.commit()
            STATE_CACHE[chat_id] = "AWAITING_LOCATION"
            send_telegram_message(chat_id, "What is the new city you want to track?")
        else:
            status_msg = f"You are currently tracking weather for: <b>{user.city}</b>.\n\nReply 'change' to update it."
            send_telegram_message(chat_id, status_msg)
        return {"status": "ok"}

    return {"status": "error"}  