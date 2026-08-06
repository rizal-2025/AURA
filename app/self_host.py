"""Start the audited Windows self-host runtime on loopback only."""

from __future__ import annotations

import argparse
import os

from app.core.self_host_validation import (
    SELF_HOST_PROFILES,
    SelfHostConfigurationError,
    validate_self_host_runtime,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start loopback-only AURA.")
    parser.add_argument("--profile", required=True, choices=tuple(SELF_HOST_PROFILES))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.environ["AURA_DISABLE_DOTENV"] = "1"
    try:
        runtime = validate_self_host_runtime(
            profile=args.profile,
            app_env=os.environ.get("APP_ENV"),
            bind_host=os.environ.get("AURA_BIND_HOST", "127.0.0.1"),
            port=os.environ.get("AURA_PORT", str(SELF_HOST_PROFILES[args.profile])),
            database_url=os.environ.get("DEMO_DATABASE_URL"),
        )
        from app.core.config import get_application_settings

        get_application_settings()
    except Exception as error:
        code = (
            str(error)
            if isinstance(error, SelfHostConfigurationError)
            else "AURA_CONFIGURATION_INVALID"
        )
        print(code)
        return 1

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=runtime.host,
        port=runtime.port,
        workers=1,
        reload=False,
        access_log=False,
        log_level="info",
        timeout_graceful_shutdown=25,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
