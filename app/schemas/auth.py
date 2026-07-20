from datetime import datetime

from pydantic import BaseModel


class GuestTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
