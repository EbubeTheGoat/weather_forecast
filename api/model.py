import sqlalchemy
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime
from api.database import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=True)
    phone_number = Column(String, unique=True, nullable=False) # Used for Telegram Chat ID
    city = Column(String, nullable=True)
    state = Column(String, nullable=True)
    country = Column(String, nullable=True, default="Unknown")
    
    # State machine tracking for Telegram
    current_step = Column(String, default="IDLE")
    notification_topic = Column(String, nullable=True)

class RegistrationLead(Base):
    __tablename__ = "registration_leads"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    created_at = Column(DateTime, server_default=sqlalchemy.sql.func.now())

class SentContent(Base):
    __tablename__ = "sent_content"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    sent_at = Column(DateTime, server_default=sqlalchemy.sql.func.now())

