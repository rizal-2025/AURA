from pydantic import BaseModel


class ReservationCreate(BaseModel):
    name: str
    people: int
    date: str
    time: str


class ReservationResponse(ReservationCreate):
    id: int
    status: str

    model_config = {
        "from_attributes": True
    }