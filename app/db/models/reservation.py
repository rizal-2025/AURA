from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from app.db.base import Base


class Reservation(Base):

    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    name: Mapped[str] = mapped_column(String(100))

    people: Mapped[int] = mapped_column(Integer)

    date: Mapped[str] = mapped_column(String(20))

    time: Mapped[str] = mapped_column(String(10))

    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
    )