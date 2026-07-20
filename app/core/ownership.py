"""Fail-closed ownership checks for V1.5 reservation operations."""


class MissingOwnerCustomerError(ValueError):
    """Raised when a secure reservation operation has no authenticated owner."""


def require_owner_customer_id(owner_customer_id):
    """Reject missing ownership before a query can target legacy NULL records."""
    if owner_customer_id is None:
        raise MissingOwnerCustomerError(
            "Authenticated customer ownership is required for this operation."
        )
    return owner_customer_id
