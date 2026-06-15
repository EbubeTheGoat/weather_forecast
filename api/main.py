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
from fastapi.responses import FileResponse
from pathlib import Path

# Initialize DB Models
Base.metadata.create_all(bind=engine)
logger = get_logger("main")

LIMITER = Limiter(key_func=get_remote_address, default_limits=["1000/hour"])
STATIC_DIR = Path(__file__).parent / "static"
# Simple in-memory cache for state management
VALID_STATES = {"IDLE", "AWAITING_CITY", "AWAITING_STATE", "AWAITING_COUNTRY", "CONFIRMED"}

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application starting up...")
    
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text("""
            UPDATE users 
            SET current_step = 'IDLE' 
            WHERE current_step NOT IN ('IDLE', 'AWAITING_CITY', 'AWAITING_STATE', 'AWAITING_COUNTRY', 'CONFIRMED')
            OR current_step IS NULL
        """))
        conn.commit()
    logger.info("Stale states cleaned up")
    
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

@app.get("/", include_in_schema=False)
def serve_landing_page():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return Response(content="Error: index.html not found", status_code=404)
    return FileResponse(index_file)
 
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

    # Fetch user or create if new
    user = db.query(api.model.User).filter(api.model.User.phone_number == chat_id).first()

    if not user:
        user = api.model.User(phone_number=chat_id, current_step="IDLE")
        db.add(user)
        db.commit()
    logger.info(f"DEBUG — chat_id={chat_id}, state='{user.current_step}', incoming='{incoming}'")

    # Prefer cache, fallback to database state
    state = user.current_step if user.current_step in VALID_STATES else "IDLE"

    # 1. IDLE STATE: User just joined or started
    if state == "IDLE":
        msg = "🚀 Welcome to Weather forecast!\n\nWhat city do you want the forecast for?"
        if send_telegram_message(chat_id, msg):
            user.current_step = "AWAITING_CITY"
            db.commit()
            return {"status": "ok"}
        return {"status": "failed_to_send"}

    # 2. AWAITING CITY STATE
    if state == "AWAITING_CITY":
        if len(incoming) < 2 or incoming.lower() == "/start":
            send_telegram_message(chat_id, "Please enter a valid city name.")
            return {"status": "ok"}

        # Save city and immediately prompt for State
        user.city = incoming
        msg = "📍 Great! What state/region do you reside in?"
        
        if send_telegram_message(chat_id, msg):
            user.current_step = "AWAITING_STATE"
            db.commit()
            return {"status": "ok"}
        return {"status": "failed_to_send"}

    # 3. AWAITING STATE/REGION STATE
    if state == "AWAITING_STATE":
        if len(incoming) < 2 or incoming.lower() == "/start":
            send_telegram_message(chat_id, "Please enter a valid state name.")
            return {"status": "ok"}

        # Save state and immediately prompt for Country
        user.state = incoming
        msg = "🌍 Excellent. What country do you reside in?"
        
        if send_telegram_message(chat_id, msg):
            user.current_step = "AWAITING_COUNTRY"
            db.commit()
            return {"status": "ok"}
        return {"status": "failed_to_send"}

    # 4. AWAITING COUNTRY STATE
    if state == "AWAITING_COUNTRY":
        if len(incoming) < 2 or incoming.lower() == "/start":
            send_telegram_message(chat_id, "Please enter a valid country name.")
            return {"status": "ok"}

        # Save country and finish onboarding
        user.country = incoming
        user.current_step = "CONFIRMED"
        db.commit()

        confirm_msg = f"✅ Onboarding Complete! You'll receive weather updates for {user.city}, {user.state}, {incoming}.\n\nReply 'change' anytime to update your location."
        
        if send_telegram_message(chat_id, confirm_msg):
            return {"status": "ok"}
        return {"status": "failed_to_send"}

    # 5. COMPLETED / CONFIRMED STATE
    if state == "CONFIRMED":
        if incoming.lower() == "change":
            msg = "🔄 Let's update it. What city do you want to track?"
            if send_telegram_message(chat_id, msg):
                user.current_step = "AWAITING_CITY"
                db.commit()
                return {"status": "ok"}
            return {"status": "failed_to_send"}
        else:
            status_msg = f"You are currently tracking weather for: {user.city}, {user.state}, {user.country}.\n\nReply 'change' to update it."
            if send_telegram_message(chat_id, status_msg):
                return {"status": "ok"}
            return {"status": "failed_to_send"}

    return {"status": "error"}
