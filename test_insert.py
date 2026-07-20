"""Disabled legacy insertion script.

V1.5 reservations must be created through an authenticated API path so the
server supplies owner_customer_id. This script intentionally performs no write.
"""


if __name__ == "__main__":
    raise SystemExit(
        "Legacy direct insertion is disabled. Use POST /reservation/ with a bearer token."
    )
