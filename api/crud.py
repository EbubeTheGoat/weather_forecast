from sqlalchemy.orm import Session
from . import model, schemas

def create_user(db: Session, user: schemas.UserBase):
    db_user = model.User(name=user.name, city=user.city, state=user.state)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def get_user(db: Session, user_id: int):
    return db.query(model.User).filter(model.User.id == user_id).first()    