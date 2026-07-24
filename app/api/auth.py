from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.security import create_customer_access_token
from app.core.unit_of_work import UnitOfWork
from app.db.database import get_db
from app.db.models.customer import Customer
from app.schemas.auth import GuestTokenResponse


router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post(
    "/guest",
    response_model=GuestTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_guest_customer(
    response: Response,
    db: Session = Depends(get_db),
) -> GuestTokenResponse:
    """Issue an anonymous customer token without accepting client identity input."""
    customer_id = uuid4()
    token_version = 1
    try:
        access_token, expires_at = create_customer_access_token(
            customer_id,
            token_version,
        )
    except RuntimeError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Layanan identitas belum dikonfigurasi.",
        ) from None

    customer = Customer(id=customer_id, token_version=token_version)
    with UnitOfWork(db) as unit:
        db.add(customer)
        db.flush()
        unit.commit()
    response.headers["Cache-Control"] = "no-store"
    return GuestTokenResponse(
        access_token=access_token,
        expires_at=expires_at,
    )
