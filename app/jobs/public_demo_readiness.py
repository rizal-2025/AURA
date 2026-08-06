"""Exit-code-only PostgreSQL readiness probe for Windows lifecycle scripts."""

from app.core.config import get_application_settings
from app.core.readiness import database_is_ready


def main() -> int:
    try:
        settings = get_application_settings()
        if settings.APP_ENV != "demo" or not database_is_ready():
            print("AURA_DATABASE_NOT_READY")
            return 1
    except Exception:
        print("AURA_DATABASE_NOT_READY")
        return 1
    print("AURA_DATABASE_READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
