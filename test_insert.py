from app.db.database import SessionLocal
from app.db.repositories.reservation_repository import ReservationRepository
from app.schemas.reservation import ReservationCreate


db = SessionLocal()

repo = ReservationRepository()

reservation = ReservationCreate(
    name="Rizal",
    people=6,
    date="2026-07-14",
    time="19:00",
)

saved = repo.create(
    db=db,
    reservation=reservation,
    customer_id="manual-test-session",
)

print(saved.id)
print(saved.name)
print(saved.people)
print(saved.date)
print(saved.time)
print(saved.status)
