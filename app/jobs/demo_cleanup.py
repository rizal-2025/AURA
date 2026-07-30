"""Run one bounded demo cleanup pass for an external scheduler."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one safe internal-demo cleanup pass.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Accepted for scheduler clarity; execution is always single-run.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Maximum eligible sessions to scan (1..500).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        from app.core.config import get_application_settings
        from app.db.database import SessionLocal
        from app.services.demo_cleanup_service import (
            DemoCleanupService,
            validate_demo_cleanup_batch_size,
        )

        batch_size = validate_demo_cleanup_batch_size(args.batch_size)
        settings = get_application_settings()
        if settings.APP_ENV != "demo":
            raise RuntimeError("demo-only")
        service = DemoCleanupService(
            session_factory=SessionLocal,
            app_env=settings.APP_ENV,
        )
        summary = asyncio.run(
            service.run_once(batch_size=batch_size)
        )
    except Exception:
        print(
            json.dumps(
                {"status": "failed", "code": "DEMO_CLEANUP_FAILED"},
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {"status": "ok", **asdict(summary)},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
