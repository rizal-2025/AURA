from uuid import UUID

from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Uuid
from sqlalchemy import ForeignKey
from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from app.db.base import Base


class Reservation(Base):

    __tablename__ = "reservations"
    __table_args__ = (
        UniqueConstraint(
            "public_reference",
            name="uq_reservations_public_reference",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    name: Mapped[str] = mapped_column(String(100))

    people: Mapped[int] = mapped_column(Integer)

    date: Mapped[str] = mapped_column(String(20))

    time: Mapped[str] = mapped_column(String(10))

    customer_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    owner_customer_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("customers.id"),
        nullable=True,
    )

    public_reference: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="pending",
    )
