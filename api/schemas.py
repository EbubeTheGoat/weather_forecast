from pydantic import BaseModel

class UserBase(BaseModel):
    name: str
    city: str | None = None
    state: str | None = None