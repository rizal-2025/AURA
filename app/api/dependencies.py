from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import InvalidCustomerToken, validate_customer_access_token
from app.db.database import get_db
from app.db.models.customer import Customer


bearer_scheme = HTTPBearer(auto_error=False)
UNAUTHORIZED_DETAIL = "Identitas pelanggan tidak valid atau telah kedaluwarsa."


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=UNAUTHORIZED_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_customer(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> Customer:
    """Resolve a trusted customer from a validated bearer token."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()

    try:
        customer_id, token_version = validate_customer_access_token(
            credentials.credentials
        )
    except (InvalidCustomerToken, RuntimeError):
        raise _unauthorized() from None

    customer = db.get(Customer, customer_id)
    if (
        customer is None
        or not customer.is_active
        or customer.token_version != token_version
    ):
        raise _unauthorized()

    return customer
