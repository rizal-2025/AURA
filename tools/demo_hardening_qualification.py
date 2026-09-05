"""Bounded offline qualification; opt-in PostgreSQL via the official environment."""

import argparse
import json
import os
from pathlib import Path
import time
import unittest


class QualificationResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed = 0

    def addSuccess(self, test):
        self.passed += 1
        super().addSuccess(test)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=("broad", "postgresql", "postgresql-full", "critical"), required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if root != Path.cwd().resolve() or os.environ.get("APP_ENV") != "test" or os.environ.get("AURA_DISABLE_DOTENV") != "1":
        raise RuntimeError("ISOLATED_TEST_ENVIRONMENT_REQUIRED")
    if args.report.resolve().is_relative_to(root):
        raise RuntimeError("EVIDENCE_MUST_BE_OUTSIDE_SOURCE_TREE")
    if args.report.exists():
        raise RuntimeError("REFUSING_TO_OVERWRITE_EVIDENCE")
    evidence = args.report.with_suffix(".scenarios.jsonl")
    with evidence.open("x", encoding="utf-8"):
        pass
    os.environ["AURA_SIMULATION_EVIDENCE"] = str(evidence)
    os.environ["AURA_SIMULATION_SEED"] = str(args.seed)
    excluded = {"test_demo_cleanup_job", "test_demo_cleanup_task_windows_roundtrip",
                "test_postgresql_test_runner", "test_uat_preflight"}
    if args.suite == "broad":
        names = ["tests." + p.stem for p in sorted((root / "tests").glob("test_*.py"))
                 if "windows" not in p.stem and p.stem not in excluded]
    elif args.suite == "critical":
        names = ["tests." + name for name in (
            "test_demo_hardening", "test_demo_blocker_fix", "test_postgresql_fixture_clock",
            "test_demo_conversation_simulation", "test_persisted_reservation_update",
            "test_g1d_transaction_ownership", "test_past_reservation_date", "test_update_reservation",
            "test_indonesian_nlu", "test_public_reference_workflow_v2")]
    else:
        from tools.postgresql_test_preflight import run_preflight
        run_preflight()
        names = ["tests.integration." + name for name in (
            "test_public_reservation_api_postgresql", "test_demo_reservation_reset_postgresql",
            "test_demo_conversation_hardening_postgresql")]
        if args.suite == "postgresql-full":
            # Explicitly reviewed disposable DB suites. Do not use unrestricted
            # top-level discovery, which includes host/Scheduler operations.
            names += ["tests.integration." + name for name in (
                "test_public_reference_workflow_v2_postgresql", "test_g1d_transactions_postgresql",
                "test_g1d_a2_restart_recovery_postgresql", "test_g1d_a2_memory_publication_postgresql",
                "test_conversation_serialization_postgresql", "test_demo_chat_postgresql",
                "test_demo_persistence_postgresql", "test_public_reservation_reference_postgresql",
                "test_demo_session_service_postgresql", "test_demo_rate_limit_cleanup_postgresql",
                "test_owner_notifications_postgresql", "test_support_tickets_postgresql",
                "test_telegram_identities_postgresql", "test_telegram_owner_commands_postgresql")]
    loader = unittest.defaultTestLoader
    suite = loader.loadTestsFromNames(names)
    discovered = suite.countTestCases()
    is_postgresql = args.suite.startswith("postgresql")
    if is_postgresql:
        from tests.integration.test_public_reservation_api_postgresql import PublicReservationAPIPostgreSQLTests as Updates
        from tests.integration.test_demo_reservation_reset_postgresql import DemoReservationResetPostgreSQLTests as Reset
        # Repetitions are recorded separately, never counted as unique scenarios.
        for _ in range(10):
            suite.addTests(Updates(name) for name in loader.getTestCaseNames(Updates)
                           if name.startswith("test_two_session_") or name == "test_waiting_writer_uses_clock_after_lock_acquisition")
            suite.addTests(Reset(name) for name in (
                "test_full_chat_wins_then_reset_clears_completed_state",
                "test_full_reset_wins_blocks_chat_and_next_chat_uses_empty_state",
                "test_chat_held_advisory_lock_makes_reset_conflict"))
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=1, buffer=True, resultclass=QualificationResult).run(suite)
    report = {"suite": args.suite, "seed": args.seed, "discovered": discovered, "run": result.testsRun,
              "passed": result.passed,
              "failed": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
              "skip_reasons": [reason for _, reason in result.skipped],
              "failure_ids": [test.id() for test, _ in result.failures + result.errors],
              "seconds": round(time.monotonic() - started, 3), "source_root": str(root),
              "curated_dialogues": 100, "stateful_traces_per_seed": 100,
              "forced_interleaving_repetitions": 80 if is_postgresql else 0}
    with args.report.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(report))
    return 0 if result.wasSuccessful() and not (is_postgresql and result.skipped) else 1


if __name__ == "__main__":
    raise SystemExit(main())
