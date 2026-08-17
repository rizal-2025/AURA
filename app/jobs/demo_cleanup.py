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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report bounded eligible-row counts without deleting data.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        from app.core.config import get_environment_settings
        from app.db.database import SessionLocal
        from app.services.demo_cleanup_service import (
            DemoCleanupService,
            validate_demo_cleanup_batch_size,
        )

        batch_size = validate_demo_cleanup_batch_size(args.batch_size)
        settings = get_environment_settings()
        if settings.APP_ENV != "demo":
            raise RuntimeError("demo-only")
        service = DemoCleanupService(
            session_factory=SessionLocal,
            app_env=settings.APP_ENV,
        )
        if args.dry_run:
            summary = service.dry_run_once(batch_size=batch_size)
        else:
            summary = asyncio.run(
                service.run_once(batch_size=batch_size)
            )
    except Exception:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": "DEMO_CLEANUP_FAILED",
                    "mode": "dry-run" if args.dry_run else "execute",
                    "eligible_sessions": 0,
                    "attempted_sessions": 0,
                    "successful_cleanup_count": 0,
                    "failed_cleanup_count": 0,
                },
                sort_keys=True,
            )
        )
        return 1

    if args.dry_run:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "mode": "dry-run",
                    "attempted_sessions": 0,
                    "successful_cleanup_count": 0,
                    "failed_cleanup_count": 0,
                    **asdict(summary),
                },
                sort_keys=True,
            )
        )
        return 0

    status = "ok"
    code = None
    exit_code = 0
    if summary.failed_sessions > 0:
        status = "failed"
        if summary.cleaned_sessions > 0:
            code = "DEMO_CLEANUP_PARTIAL_FAILURE"
            exit_code = 2
        else:
            code = "DEMO_CLEANUP_FAILED"
            exit_code = 1
    payload = {
        "status": status,
        "mode": "execute",
        "eligible_sessions": summary.scanned,
        "attempted_sessions": summary.scanned,
        "successful_cleanup_count": summary.cleaned_sessions,
        "failed_cleanup_count": summary.failed_sessions,
        **asdict(summary),
    }
    if code is not None:
        payload["code"] = code
    print(
        json.dumps(
            payload,
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
