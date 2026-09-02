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
PARSER = WINDOWS_ROOT / "Get-AuraProviderRuntimeEvents.ps1"
EVENT_PATH_ENV = "AURA_PROVIDER_RUNTIME_EVENT_LOG_PATH"
MODEL = "gpt-test-runtime-events"
REQUEST_IDS = (
    "61d831fc-2708-4693-a008-3f09f906be7a",
    "8e7eac2b-406a-4483-9c32-c1697e98a7bc",
    "f20ed76c-f6a5-4dd0-b337-12be0e615dc4",
)


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
        self.temporary_paths = []
        self.previous_logger_level = logger.level
        logger.setLevel(logging.INFO)

    def tearDown(self):
        for handler in tuple(logger.handlers):
            if isinstance(handler, ProviderRuntimeEventFileHandler):
                logger.removeHandler(handler)
                handler.close()
        for path in self.temporary_paths:
            path.unlink(missing_ok=True)
        logger.setLevel(self.previous_logger_level)

    def new_path(self) -> Path:
        descriptor, name = tempfile.mkstemp(
            prefix=".provider-runtime-event-",
            suffix=".jsonl",
            dir=PROJECT_ROOT,
        )
        os.close(descriptor)
        path = Path(name)
        self.temporary_paths.append(path)
        return path

    def configure(self, path: Path) -> ProviderRuntimeEventFileHandler:
        with patch.dict(os.environ, {EVENT_PATH_ENV: str(path)}, clear=False):
            configure_provider_runtime_event_logging()
            configure_provider_runtime_event_logging()
        handlers = [
            handler
            for handler in logger.handlers
            if isinstance(handler, ProviderRuntimeEventFileHandler)
        ]
        self.assertEqual(len(handlers), 1)
        return handlers[0]

    def test_absent_path_preserves_stream_only_behavior(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(EVENT_PATH_ENV, None)
            configure_provider_runtime_event_logging()
        self.assertFalse(
            any(
                isinstance(handler, ProviderRuntimeEventFileHandler)
                for handler in logger.handlers
            )
        )

    def test_configured_path_attaches_exactly_one_append_handler(self):
        path = self.new_path()
        handler = self.configure(path)
        self.assertEqual(handler.mode, "a")
        self.assertEqual(Path(handler.baseFilename), path)

    def test_three_event_schemas_persist_once_without_payload_content(self):
        path = self.new_path()
        self.configure(path)
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
        for handler in logger.handlers:
            if isinstance(handler, ProviderRuntimeEventFileHandler):
                handler.flush()

        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        persisted = [json.loads(line) for line in lines]
        self.assertEqual(
            [item["event"] for item in persisted],
            [item["event"] for item in events],
        )
        self.assertEqual(
            {item["request_id"] for item in persisted},
            {REQUEST_IDS[0]},
        )
        self.assertTrue(all(item["timestamp"].endswith("Z") for item in persisted))
        rendered = path.read_text(encoding="utf-8")
        for forbidden in (
            "prompt",
            "response body",
            "Authorization",
            "OPENAI_API_KEY",
            "sk-test-not-real",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_unrelated_or_extended_records_are_not_persisted(self):
        path = self.new_path()
        self.configure(path)
        logger.info("timestamp=2026-09-02T00:00:00Z stage=START code=READY")
        unsafe = provider_event("AI_PROVIDER_ATTEMPT", REQUEST_IDS[0])
        unsafe["prompt"] = "obvious private payload"
        logger.info(json.dumps(unsafe))
        for handler in logger.handlers:
            if isinstance(handler, ProviderRuntimeEventFileHandler):
                handler.flush()
        self.assertEqual(path.read_text(encoding="utf-8"), "")

    def test_actual_provider_success_persists_one_attempt_and_one_outcome(self):
        path = self.new_path()
        self.configure(path)
        config = SimpleNamespace(
            OPENAI_API_KEY="sk-offline-runtime-event-test-not-real",
            OPENAI_MODEL=MODEL,
            AI_PROVIDER_TIMEOUT_SECONDS=20,
        )
        with patch("app.services.ai.openai_provider.AsyncOpenAI"):
            provider = OpenAIProvider(config)
        create = AsyncMock(return_value=SimpleNamespace(output_text="safe result"))
        provider.client = SimpleNamespace(
            responses=SimpleNamespace(create=create)
        )

        result = asyncio.run(
            provider.chat("private prompt", request_id=REQUEST_IDS[0])
        )
        self.assertEqual(result, "safe result")
        create.assert_awaited_once()
        for handler in logger.handlers:
            if isinstance(handler, ProviderRuntimeEventFileHandler):
                handler.flush()

        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        events = [json.loads(line) for line in lines]
        self.assertEqual(
            [event["event"] for event in events],
            ["AI_PROVIDER_ATTEMPT", "AI_PROVIDER_OUTCOME"],
        )
        self.assertEqual(events[1]["outcome"], "SUCCESS")
        rendered = path.read_text(encoding="utf-8")
        self.assertNotIn("private prompt", rendered)
        self.assertNotIn("safe result", rendered)
        self.assertNotIn(config.OPENAI_API_KEY, rendered)

    def test_invalid_or_conflicting_path_fails_closed(self):
        first = self.new_path()
        missing = Path(f"{first}.missing")
        with patch.dict(os.environ, {EVENT_PATH_ENV: str(missing)}, clear=False):
            with self.assertRaisesRegex(
                RuntimeError, "AURA_PROVIDER_RUNTIME_EVENT_PATH_INVALID"
            ):
                configure_provider_runtime_event_logging()

        second = self.new_path()
        self.configure(first)
        with patch.dict(os.environ, {EVENT_PATH_ENV: str(second)}, clear=False):
            with self.assertRaisesRegex(
                RuntimeError, "AURA_PROVIDER_RUNTIME_EVENT_HANDLER_CONFLICT"
            ):
                configure_provider_runtime_event_logging()


@unittest.skipUnless(os.name == "nt", "PowerShell parser tests require Windows")
class ProviderRuntimeEventPowerShellTests(unittest.TestCase):
    def invoke_parser(self, lines: list[str], request_id: str):
        payload = base64.b64encode("\n".join(lines).encode("utf-8")).decode("ascii")
        command = rf"""
. '{COMMON}'
$text = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String($env:AURA_TEST_EVENT_LINES)
)
$lines = if ($text.Length -eq 0) {{ @() }} else {{ @($text -split "`n") }}
$events = @(ConvertFrom-AuraProviderRuntimeEventLines `
    -Lines $lines -RequestId '{request_id}')
[PSCustomObject]@{{
    attempts = @($events | Where-Object event -ceq 'AI_PROVIDER_ATTEMPT').Count
    outcomes = @($events | Where-Object event -ceq 'AI_PROVIDER_OUTCOME').Count
    fallbacks = @($events | Where-Object event -ceq 'AI_PROVIDER_FALLBACK').Count
    requestIds = @($events.request_id | Sort-Object -Unique)
}} | ConvertTo-Json -Compress
"""
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
            env={**os.environ, "AURA_TEST_EVENT_LINES": payload},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    @staticmethod
    def line(event: dict, second: int) -> str:
        return json.dumps(
            {
                **event,
                "timestamp": f"2026-09-02T01:02:{second:02d}.123Z",
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def test_six_call_fixture_is_correlated_two_two_two(self):
        lines = []
        second = 0
        for request_id in REQUEST_IDS:
            for _ in range(2):
                lines.append(
                    self.line(
                        provider_event("AI_PROVIDER_ATTEMPT", request_id), second
                    )
                )
                second += 1
                lines.append(
                    self.line(
                        provider_event(
                            "AI_PROVIDER_OUTCOME",
                            request_id,
                            elapsed_ms=10,
                            outcome="SUCCESS",
                        ),
                        second,
                    )
                )
                second += 1

        aggregate_attempts = 0
        for request_id in REQUEST_IDS:
            result = self.invoke_parser(lines, request_id)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            parsed = json.loads(result.stdout)
            self.assertEqual(parsed["attempts"], 2)
            self.assertEqual(parsed["outcomes"], 2)
            self.assertEqual(parsed["fallbacks"], 0)
            self.assertEqual(parsed["requestIds"], [request_id])
            aggregate_attempts += parsed["attempts"]
        self.assertEqual(aggregate_attempts, 6)

    def test_malformed_lifecycle_or_cross_request_record_fails_closed(self):
        valid = self.line(
            provider_event("AI_PROVIDER_ATTEMPT", REQUEST_IDS[0]), 0
        )
        cases = (
            "not-json",
            "timestamp=2026-09-02T01:02:00Z stage=START code=READY",
            json.dumps(
                {
                    **json.loads(valid),
                    "request_id": REQUEST_IDS[0].upper(),
                }
            ),
            json.dumps({**json.loads(valid), "prompt": "private"}),
        )
        for malformed in cases:
            with self.subTest(malformed=malformed[:20]):
                result = self.invoke_parser([valid, malformed], REQUEST_IDS[0])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "AURA_PROVIDER_RUNTIME_EVENT_RECORD_INVALID",
                    result.stderr,
                )


@unittest.skipUnless(
    windows_is_administrator(),
    "Disposable protected-ACL test requires Windows Administrator",
)
class ProviderRuntimeEventAclPowerShellTests(unittest.TestCase):
    def test_sink_file_is_protected_and_broad_acl_is_rejected(self):
        command = rf"""
. '{COMMON}'
$testRoot = Join-Path ([IO.Path]::GetTempPath()) (
    'aura-provider-runtime-acl-' + [Guid]::NewGuid().ToString('N')
)
$eventPath = Join-Path $testRoot 'provider-runtime-events.jsonl'
try {{
    [void](New-Item -ItemType Directory -Path $testRoot)
    Set-AuraOperatorProtectedAcl -Path $testRoot -Container
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $inheritance = (
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    )
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    [void]$acl.AddAccessRule(
        [Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
            [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
    )
    [void]$acl.AddAccessRule(
        [Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'),
            [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
    )
    [void]$acl.AddAccessRule(
        [Security.AccessControl.FileSystemAccessRule]::new(
            $currentSid,
            ([Security.AccessControl.FileSystemRights]::Modify -bor
                [Security.AccessControl.FileSystemRights]::Synchronize),
            $inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
    )
    [IO.Directory]::SetAccessControl($testRoot, $acl)
    $script:AuraLogRoot = $testRoot
    $script:AuraProviderRuntimeEventLog = $eventPath
    function Initialize-AuraDataDirectories {{ }}

    $actual = Initialize-AuraProviderRuntimeEventSink
    if ($actual -cne $eventPath) {{ throw 'sink-path-invalid' }}
    Assert-AuraOperatorSecretAcl -Path $eventPath

    $unsafeAcl = Get-Acl -LiteralPath $eventPath
    [void]$unsafeAcl.AddAccessRule(
        [Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545'),
            [Security.AccessControl.FileSystemRights]::ReadAndExecute,
            [Security.AccessControl.AccessControlType]::Allow
        )
    )
    [IO.File]::SetAccessControl($eventPath, $unsafeAcl)
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
    if (Test-Path -LiteralPath $eventPath -PathType Leaf) {{
        Set-AuraOperatorProtectedAcl -Path $eventPath
    }}
    if (Test-Path -LiteralPath $testRoot -PathType Container) {{
        Set-AuraOperatorProtectedAcl -Path $testRoot -Container
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }}
}}
"""
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
    def test_official_start_wires_protected_sink_before_process_creation(self):
        start = START.read_text(encoding="utf-8")
        initialize = "Initialize-AuraProviderRuntimeEventSink"
        set_environment = "$env:AURA_PROVIDER_RUNTIME_EVENT_LOG_PATH"
        create = "Invoke-CimMethod -ClassName Win32_Process -MethodName Create"
        self.assertLess(start.index(initialize), start.index(set_environment))
        self.assertLess(start.index(set_environment), start.index(create))
        self.assertIn(
            "AURA_PROVIDER_RUNTIME_EVENT_LOG_PATH = `",
            start,
        )
        self.assertIn(
            "Restore-AuraProcessEnvironment -Previous $internalPrevious",
            start,
        )

    def test_sink_uses_canonical_log_root_and_existing_acl_contract(self):
        common = COMMON.read_text(encoding="utf-8")
        self.assertIn("'provider-runtime-events.jsonl'", common)
        self.assertIn(
            "Assert-AuraOperatorRuntimeContainerAcl -Path $script:AuraLogRoot",
            common,
        )
        self.assertIn("Set-AuraOperatorProtectedAcl -Path $path", common)
        self.assertIn("Assert-AuraOperatorSecretAcl -Path $path", common)
        self.assertIn("AURA_PROVIDER_RUNTIME_EVENT_ACL_INVALID", common)

    def test_operator_parser_is_canonical_and_read_only(self):
        parser = PARSER.read_text(encoding="utf-8")
        self.assertIn("Get-AuraProviderRuntimeEvents -RequestId $RequestId", parser)
        for forbidden in (
            "Add-Content",
            "Clear-Content",
            "New-Item",
            "Remove-Item",
            "Set-Acl",
            "Set-Content",
            "WriteAll",
        ):
            self.assertNotIn(forbidden, parser)
        self.assertNotIn("production.conf", parser)
        self.assertNotIn("OPENAI_API_KEY", parser)

    def test_no_stream_redirect_or_manual_process_path_was_added(self):
        combined = START.read_text(encoding="utf-8") + PARSER.read_text(
            encoding="utf-8"
        )
        self.assertNotIn("RedirectStandardOutput", combined)
        self.assertNotIn("RedirectStandardError", combined)
        self.assertNotIn("uvicorn", combined.casefold())
        self.assertNotIn("taskkill", combined.casefold())


if __name__ == "__main__":
    unittest.main()
