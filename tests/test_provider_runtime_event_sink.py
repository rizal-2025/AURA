import asyncio
import base64
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.core.logger import (
    ProviderRuntimeEventFileHandler,
    configure_provider_runtime_event_logging,
    logger,
)
from app.services.ai.openai_provider import OpenAIProvider


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ROOT = PROJECT_ROOT / "deploy" / "windows"
COMMON = WINDOWS_ROOT / "AuraWindows.Common.ps1"
START = WINDOWS_ROOT / "Start-Aura.ps1"
READER = WINDOWS_ROOT / "Get-AuraProviderRuntimeEvents.ps1"
EVENT_PATH_ENV = "AURA_PROVIDER_RUNTIME_EVENT_LOG_PATH"
LOCK_PATH_ENV = "AURA_PROVIDER_RUNTIME_EVENT_LOCK_PATH"
MODEL = "gpt-test-runtime-events"
REQUEST_IDS = (
    "61d831fc-2708-4693-a008-3f09f906be7a",
    "8e7eac2b-406a-4483-9c32-c1697e98a7bc",
    "f20ed76c-f6a5-4dd0-b337-12be0e615dc4",
)


def offline_subprocess_environment(**extra: str) -> dict[str, str]:
    safe_names = (
        "COMSPEC",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    )
    environment = {
        name: os.environ[name]
        for name in safe_names
        if name in os.environ
    }
    environment["PYTHONPATH"] = str(PROJECT_ROOT)
    environment.update(extra)
    return environment


def provider_event(event_type: str, request_id: str, **fields) -> dict:
    event = {
        "event": event_type,
        "model": MODEL,
        "operation": "responses.create",
        "provider": "openai",
        "request_id": request_id,
    }
    event.update(fields)
    return event


def windows_is_administrator() -> bool:
    if os.name != "nt":
        return False
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())


class ProviderRuntimeEventHandlerTests(unittest.TestCase):
    def setUp(self):
        event_descriptor, event_name = tempfile.mkstemp(
            prefix=".aura-provider-event-",
            suffix=".jsonl",
            dir=PROJECT_ROOT,
        )
        os.close(event_descriptor)
        lock_descriptor, lock_name = tempfile.mkstemp(
            prefix=".aura-provider-event-",
            suffix=".lock",
            dir=PROJECT_ROOT,
        )
        os.write(lock_descriptor, b"\0")
        os.close(lock_descriptor)
        self.event_path = Path(event_name)
        self.lock_path = Path(lock_name)
        self.previous_logger_level = logger.level
        logger.setLevel(logging.INFO)

    def tearDown(self):
        for handler in tuple(logger.handlers):
            if isinstance(handler, ProviderRuntimeEventFileHandler):
                logger.removeHandler(handler)
                handler.close()
        logger.setLevel(self.previous_logger_level)
        self.event_path.unlink(missing_ok=True)
        self.lock_path.unlink(missing_ok=True)

    def configure(self):
        environment = {
            EVENT_PATH_ENV: str(self.event_path),
            LOCK_PATH_ENV: str(self.lock_path),
        }
        with patch.dict(os.environ, environment, clear=False):
            configure_provider_runtime_event_logging(logger)
            configure_provider_runtime_event_logging(logger)
        handlers = [
            handler
            for handler in logger.handlers
            if isinstance(handler, ProviderRuntimeEventFileHandler)
        ]
        self.assertEqual(len(handlers), 1)
        return handlers[0]

    def test_both_paths_are_required_and_handler_is_idempotent(self):
        with patch.dict(os.environ, {EVENT_PATH_ENV: str(self.event_path)}, clear=True):
            with self.assertRaisesRegex(
                RuntimeError,
                "AURA_PROVIDER_RUNTIME_EVENT_LOCK_PATH_INVALID",
            ):
                configure_provider_runtime_event_logging(logger)
        handler = self.configure()
        self.assertEqual(Path(handler.baseFilename), self.event_path)
        self.assertEqual(Path(handler.lockFilename), self.lock_path)

    def test_three_schemas_persist_without_payload_or_secret_fields(self):
        self.configure()
        events = (
            provider_event("AI_PROVIDER_ATTEMPT", REQUEST_IDS[0]),
            provider_event(
                "AI_PROVIDER_OUTCOME",
                REQUEST_IDS[0],
                elapsed_ms=23,
                outcome="SUCCESS",
            ),
            provider_event(
                "AI_PROVIDER_FALLBACK",
                REQUEST_IDS[0],
                locale="en-US",
                reason="TIMEOUT",
            ),
        )
        for event in events:
            logger.info(json.dumps(event, separators=(",", ":")))

        lines = self.event_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        persisted = [json.loads(line) for line in lines]
        self.assertEqual(
            [item["event"] for item in persisted],
            [item["event"] for item in events],
        )
        self.assertTrue(all(item["timestamp"].endswith("Z") for item in persisted))
        rendered = self.event_path.read_text(encoding="utf-8")
        for forbidden in (
            "prompt",
            "response body",
            "Authorization",
            "OPENAI_API_KEY",
            "sk-test-not-real",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_unrelated_extended_and_noncanonical_records_are_rejected(self):
        self.configure()
        logger.info("ordinary application log")
        extended = provider_event("AI_PROVIDER_ATTEMPT", REQUEST_IDS[0])
        extended["prompt"] = "private"
        logger.info(json.dumps(extended))
        uppercase = provider_event(
            "AI_PROVIDER_ATTEMPT",
            REQUEST_IDS[0].upper(),
        )
        logger.info(json.dumps(uppercase))
        self.assertEqual(self.event_path.read_bytes(), b"")

    def test_mocked_provider_call_persists_one_attempt_and_one_outcome(self):
        self.configure()
        config = SimpleNamespace(
            OPENAI_API_KEY="sk-offline-test-not-real",
            OPENAI_MODEL=MODEL,
            AI_PROVIDER_TIMEOUT_SECONDS=20,
        )
        with patch("app.services.ai.openai_provider.AsyncOpenAI"):
            provider = OpenAIProvider(config)
        create = AsyncMock(return_value=SimpleNamespace(output_text="safe result"))
        provider.client = SimpleNamespace(responses=SimpleNamespace(create=create))

        result = asyncio.run(
            provider.chat("private prompt", request_id=REQUEST_IDS[0])
        )
        self.assertEqual(result, "safe result")
        create.assert_awaited_once()
        events = [
            json.loads(line)
            for line in self.event_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [event["event"] for event in events],
            ["AI_PROVIDER_ATTEMPT", "AI_PROVIDER_OUTCOME"],
        )
        self.assertEqual(events[1]["outcome"], "SUCCESS")
        rendered = self.event_path.read_text(encoding="utf-8")
        self.assertNotIn("private prompt", rendered)
        self.assertNotIn("safe result", rendered)
        self.assertNotIn(config.OPENAI_API_KEY, rendered)

    @unittest.skipUnless(os.name == "nt", "Windows inter-process lock contract")
    def test_four_concurrent_processes_preserve_exact_count_in_three_trials(self):
        worker = r'''
import json
import logging
from pathlib import Path
import sys
from uuid import UUID
from app.core.provider_runtime_events import (
    ProviderRuntimeEventFileHandler,
    ProviderRuntimeEventFilter,
    ProviderRuntimeEventFormatter,
)
event_path, lock_path, offset, count = sys.argv[1:]
handler = ProviderRuntimeEventFileHandler(Path(event_path), Path(lock_path))
handler.addFilter(ProviderRuntimeEventFilter())
handler.setFormatter(ProviderRuntimeEventFormatter())
worker_logger = logging.getLogger("aura-concurrent-writer-" + offset)
worker_logger.propagate = False
worker_logger.setLevel(logging.INFO)
worker_logger.addHandler(handler)
for value in range(int(count)):
    request_id = str(UUID(int=int(offset) + value + 1))
    event = {
        "event": "AI_PROVIDER_ATTEMPT",
        "model": "gpt-test-runtime-events",
        "operation": "responses.create",
        "provider": "openai",
        "request_id": request_id,
    }
    worker_logger.info(json.dumps(event, separators=(",", ":")))
handler.close()
'''
        for trial in range(3):
            with self.subTest(trial=trial):
                self.event_path.write_bytes(b"")
                writers = [
                    subprocess.Popen(
                        [
                            str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"),
                            "-c",
                            worker,
                            str(self.event_path),
                            str(self.lock_path),
                            str(trial * 100_000 + index * 10_000),
                            "50",
                        ],
                        cwd=PROJECT_ROOT,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=offline_subprocess_environment(),
                    )
                    for index in range(4)
                ]
                for process in writers:
                    stdout, stderr = process.communicate(timeout=30)
                    self.assertEqual(process.returncode, 0, stdout + stderr)

                lines = self.event_path.read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(lines), 200)
                records = [json.loads(line) for line in lines]
                self.assertEqual(
                    len({record["request_id"] for record in records}),
                    200,
                )
                self.assertTrue(
                    all(
                        record["event"] == "AI_PROVIDER_ATTEMPT"
                        for record in records
                    )
                )


@unittest.skipUnless(os.name == "nt", "PowerShell contract requires Windows")
class ProviderRuntimeEventPowerShellTests(unittest.TestCase):
    @staticmethod
    def line(event: dict, second: int) -> str:
        return json.dumps(
            {
                **event,
                "timestamp": f"2026-09-03T01:02:{second:02d}.123Z",
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def invoke_converter(
        self,
        lines: list[str],
        request_id: str,
        not_before: str = "2026-09-03T01:02:00.000Z",
    ):
        payload = base64.b64encode("\n".join(lines).encode("utf-8")).decode("ascii")
        command = rf'''
. '{COMMON}'
$text = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String($env:AURA_TEST_EVENT_LINES)
)
$lines = if ($text.Length -eq 0) {{ @() }} else {{ @($text -split "`n") }}
$events = @(ConvertFrom-AuraProviderRuntimeEventLines `
    -Lines $lines -RequestId '{request_id}' `
    -NotBeforeUtc '{not_before}' -MaxRecords 8)
[PSCustomObject]@{{
    attempts = @($events | Where-Object event -ceq 'AI_PROVIDER_ATTEMPT').Count
    outcomes = @($events | Where-Object event -ceq 'AI_PROVIDER_OUTCOME').Count
    fallbacks = @($events | Where-Object event -ceq 'AI_PROVIDER_FALLBACK').Count
    requestIds = @($events | ForEach-Object {{ $_.request_id }} | `
        Sort-Object -Unique)
}} | ConvertTo-Json -Compress
'''
        return subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            cwd=PROJECT_ROOT,
            env=offline_subprocess_environment(AURA_TEST_EVENT_LINES=payload),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_request_ids_and_time_window_remain_separable(self):
        lines = [
            self.line(provider_event("AI_PROVIDER_ATTEMPT", REQUEST_IDS[0]), 0),
            self.line(
                provider_event(
                    "AI_PROVIDER_OUTCOME",
                    REQUEST_IDS[0],
                    elapsed_ms=10,
                    outcome="SUCCESS",
                ),
                1,
            ),
            self.line(provider_event("AI_PROVIDER_ATTEMPT", REQUEST_IDS[1]), 2),
            self.line(
                provider_event(
                    "AI_PROVIDER_FALLBACK",
                    REQUEST_IDS[1],
                    locale="id-ID",
                    reason="TIMEOUT",
                ),
                3,
            ),
        ]
        first = self.invoke_converter(lines, REQUEST_IDS[0])
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        parsed = json.loads(first.stdout)
        self.assertEqual(parsed["attempts"], 1)
        self.assertEqual(parsed["outcomes"], 1)
        self.assertEqual(parsed["fallbacks"], 0)
        self.assertEqual(parsed["requestIds"], [REQUEST_IDS[0]])

        after = self.invoke_converter(
            lines,
            REQUEST_IDS[0],
            "2026-09-03T01:02:01.500Z",
        )
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
        self.assertEqual(json.loads(after.stdout)["attempts"], 0)
        self.assertEqual(json.loads(after.stdout)["outcomes"], 0)

    def test_malformed_and_incomplete_records_fail_closed(self):
        valid = self.line(provider_event("AI_PROVIDER_ATTEMPT", REQUEST_IDS[0]), 0)
        cases = (
            "not-json",
            valid[:-1],
            json.dumps({**json.loads(valid), "prompt": "private"}),
            json.dumps({**json.loads(valid), "request_id": REQUEST_IDS[0].upper()}),
        )
        for malformed in cases:
            with self.subTest(malformed=malformed[:20]):
                result = self.invoke_converter([valid, malformed], REQUEST_IDS[0])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "AURA_PROVIDER_RUNTIME_EVENT_RECORD_INVALID",
                    result.stderr,
                )

    def test_matching_result_limit_fails_closed(self):
        lines = [
            self.line(provider_event("AI_PROVIDER_ATTEMPT", REQUEST_IDS[0]), value)
            for value in range(9)
        ]
        result = self.invoke_converter(lines, REQUEST_IDS[0])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "AURA_PROVIDER_RUNTIME_EVENT_RESULT_LIMIT_EXCEEDED",
            result.stderr,
        )

    def test_readback_waits_for_writer_lock_and_returns_complete_snapshot(self):
        event_descriptor, event_name = tempfile.mkstemp(
            prefix=".aura-provider-readback-",
            suffix=".jsonl",
            dir=PROJECT_ROOT,
        )
        lock_descriptor, lock_name = tempfile.mkstemp(
            prefix=".aura-provider-readback-",
            suffix=".lock",
            dir=PROJECT_ROOT,
        )
        os.close(event_descriptor)
        os.write(lock_descriptor, b"\0")
        os.close(lock_descriptor)
        event_path = Path(event_name)
        lock_path = Path(lock_name)
        event_path.write_text(
            self.line(provider_event("AI_PROVIDER_ATTEMPT", REQUEST_IDS[0]), 0)
            + "\n",
            encoding="utf-8",
        )
        holder_code = r'''
import sys
import time
from pathlib import Path
from app.core.provider_runtime_events import ProviderRuntimeEventFileHandler
handler = ProviderRuntimeEventFileHandler(Path(sys.argv[2]), Path(sys.argv[1]))
with open(sys.argv[1], "r+b", buffering=0) as lock_file:
    handler._acquire_lock(lock_file)
    print("LOCKED", flush=True)
    time.sleep(0.5)
    handler._release_lock(lock_file)
'''
        holder = subprocess.Popen(
            [
                str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"),
                "-c",
                holder_code,
                str(lock_path),
                str(event_path),
            ],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=offline_subprocess_environment(),
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "LOCKED")
            command = rf'''
. '{COMMON}'
$script:AuraLogRoot = '{event_path.parent}'
$script:AuraProviderRuntimeEventLog = '{event_path}'
$script:AuraProviderRuntimeEventLock = '{lock_path}'
function Assert-AuraOperatorRuntimeContainerAcl {{ }}
function Assert-AuraOperatorSecretAcl {{ }}
@(Get-AuraProviderRuntimeEvents -RequestId '{REQUEST_IDS[0]}' `
    -NotBeforeUtc '2026-09-03T01:02:00.000Z' -MaxRecords 4) `
    | ConvertTo-Json -Compress
'''
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            record = json.loads(result.stdout)
            self.assertEqual(record["event"], "AI_PROVIDER_ATTEMPT")
            self.assertEqual(record["request_id"], REQUEST_IDS[0])
            stdout, stderr = holder.communicate(timeout=10)
            self.assertEqual(holder.returncode, 0, stdout + stderr)
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=10)
            event_path.unlink(missing_ok=True)
            lock_path.unlink(missing_ok=True)


@unittest.skipUnless(
    windows_is_administrator(),
    "Disposable protected-ACL test requires Windows Administrator",
)
class ProviderRuntimeEventAclPowerShellTests(unittest.TestCase):
    def test_event_and_lock_files_are_protected_and_broad_acl_is_rejected(self):
        command = rf'''
. '{COMMON}'
$testRoot = Join-Path ([IO.Path]::GetTempPath()) (
    'aura-provider-runtime-acl-' + [Guid]::NewGuid().ToString('N')
)
$eventPath = Join-Path $testRoot 'provider-runtime-events.jsonl'
$lockPath = Join-Path $testRoot 'provider-runtime-events.lock'
try {{
    [void](New-Item -ItemType Directory -Path $testRoot)
    Set-AuraOperatorProtectedAcl -Path $testRoot -Container
    $script:AuraLogRoot = $testRoot
    $script:AuraProviderRuntimeEventLog = $eventPath
    $script:AuraProviderRuntimeEventLock = $lockPath
    function Initialize-AuraDataDirectories {{ }}

    $sink = Initialize-AuraProviderRuntimeEventSink
    if ($sink.EventPath -cne $eventPath) {{ throw 'event-path-invalid' }}
    if ($sink.LockPath -cne $lockPath) {{ throw 'lock-path-invalid' }}
    Assert-AuraOperatorSecretAcl -Path $eventPath
    Assert-AuraOperatorSecretAcl -Path $lockPath
    if ((Get-Item -LiteralPath $lockPath).Length -lt 1) {{
        throw 'lock-file-empty'
    }}

    $unsafeAcl = Get-Acl -LiteralPath $lockPath
    [void]$unsafeAcl.AddAccessRule(
        [Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545'),
            [Security.AccessControl.FileSystemRights]::ReadAndExecute,
            [Security.AccessControl.AccessControlType]::Allow
        )
    )
    [IO.File]::SetAccessControl($lockPath, $unsafeAcl)
    try {{
        [void](Initialize-AuraProviderRuntimeEventSink)
        throw 'unsafe-acl-accepted'
    }} catch {{
        if ($_.Exception.Message -ceq 'unsafe-acl-accepted') {{ throw }}
        if ($_.Exception.Message -cne 'AURA_PROVIDER_RUNTIME_EVENT_ACL_INVALID') {{
            throw
        }}
    }}
    Write-Output 'AURA_PROVIDER_RUNTIME_EVENT_ACL_TEST_OK'
}} finally {{
    foreach ($path in @($eventPath, $lockPath)) {{
        if (Test-Path -LiteralPath $path -PathType Leaf) {{
            Set-AuraOperatorProtectedAcl -Path $path
        }}
    }}
    if (Test-Path -LiteralPath $testRoot -PathType Container) {{
        Set-AuraOperatorProtectedAcl -Path $testRoot -Container
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }}
}}
'''
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("AURA_PROVIDER_RUNTIME_EVENT_ACL_TEST_OK", result.stdout)


class ProviderRuntimeEventStaticContractTests(unittest.TestCase):
    def test_official_start_wires_both_paths_before_process_creation(self):
        start = START.read_text(encoding="utf-8")
        initialize = "Initialize-AuraProviderRuntimeEventSink"
        event_environment = "$env:AURA_PROVIDER_RUNTIME_EVENT_LOG_PATH"
        lock_environment = "$env:AURA_PROVIDER_RUNTIME_EVENT_LOCK_PATH"
        create = "Invoke-CimMethod -ClassName Win32_Process -MethodName Create"
        self.assertLess(start.index(initialize), start.index(event_environment))
        self.assertLess(start.index(event_environment), start.index(create))
        self.assertLess(start.index(lock_environment), start.index(create))
        self.assertIn(
            "Restore-AuraProcessEnvironment -Previous $internalPrevious",
            start,
        )

    def test_reader_is_bounded_correlated_and_read_only(self):
        reader = READER.read_text(encoding="utf-8")
        self.assertIn("-RequestId $RequestId", reader)
        self.assertIn("-NotBeforeUtc $NotBeforeUtc", reader)
        self.assertIn("-MaxRecords $MaxRecords", reader)
        for forbidden in (
            "Add-Content",
            "Clear-Content",
            "New-Item",
            "Remove-Item",
            "Set-Acl",
            "Set-Content",
            "WriteAll",
            "production.conf",
            "OPENAI_API_KEY",
        ):
            self.assertNotIn(forbidden, reader)

    def test_common_uses_protected_programdata_paths_and_shared_lock(self):
        common = COMMON.read_text(encoding="utf-8")
        self.assertIn("'provider-runtime-events.jsonl'", common)
        self.assertIn("'provider-runtime-events.lock'", common)
        self.assertIn("$lockStream.Lock(0, 1)", common)
        self.assertIn("$lockStream.Unlock(0, 1)", common)
        self.assertIn("Assert-AuraOperatorRuntimeContainerAcl", common)
        self.assertIn("Assert-AuraOperatorSecretAcl -Path $path", common)
        self.assertIn("AURA_PROVIDER_RUNTIME_EVENT_RESULT_LIMIT_EXCEEDED", common)

    def test_no_provider_semantic_or_manual_process_change(self):
        provider = (
            PROJECT_ROOT / "app" / "services" / "ai" / "openai_provider.py"
        ).read_text(encoding="utf-8")
        self.assertIn("await self.client.responses.create(**request)", provider)
        self.assertIn("max_retries=0", provider)
        combined = START.read_text(encoding="utf-8") + READER.read_text(
            encoding="utf-8"
        )
        self.assertNotIn("taskkill", combined.casefold())
        self.assertNotIn("uvicorn", combined.casefold())
        self.assertNotIn("OPENAI_MODEL", combined)
        self.assertNotIn("AI_PROVIDER=", combined)


if __name__ == "__main__":
    unittest.main()
