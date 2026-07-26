import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from app.integrations.telegram.runner import validate_runner_configuration
from tools import uat_preflight


VALID_TOKEN = "123456789:abcdefghijklmnopqrstuvwxyzABCDE"
IDENTITY_SECRET = "telegram-identity-secret-that-is-long-enough"


class FakeResult:
    def __init__(self, database_name, current_user="aura_uat_user"):
        self.row = (database_name, current_user)

    def one(self):
        return self.row


class FakeConnection:
    def __init__(self, database_name):
        self.database_name = database_name

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        return False

    def execute(self, _statement):
        return FakeResult(self.database_name)


class FakeEngine:
    def __init__(self, database_name, *, connection_error=None):
        self.database_name = database_name
        self.connection_error = connection_error
        self.disposed = False

    def connect(self):
        if self.connection_error is not None:
            raise self.connection_error
        return FakeConnection(self.database_name)

    def dispose(self):
        self.disposed = True


class FakeInspector:
    def __init__(self, tables):
        self.tables = tables

    def get_table_names(self):
        return list(self.tables)


def database_settings(url="postgresql+psycopg://hidden"):
    return SimpleNamespace(DATABASE_URL=url)


def ai_settings():
    return SimpleNamespace(
        AI_PROVIDER="ollama",
        OLLAMA_BASE_URL="http://localhost:11434/v1",
        OLLAMA_MODEL="qwen2.5:3b",
    )


def telegram_settings(**overrides):
    values = {
        "APP_ENV": "test",
        "TELEGRAM_BOT_TOKEN": VALID_TOKEN,
        "TELEGRAM_IDENTITY_SECRET": IDENTITY_SECRET,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class DatabasePreflightTests(unittest.TestCase):
    def run_database_check(
        self,
        database_name,
        *,
        tables=None,
        settings_loader=None,
        engine=None,
    ):
        output = []
        selected_engine = engine or FakeEngine(database_name)
        existing_tables = (
            set(uat_preflight.required_table_names())
            if tables is None
            else set(tables)
        )
        passed = uat_preflight.check_database(
            output=output.append,
            settings_loader=settings_loader or database_settings,
            engine_factory=lambda *_args, **_kwargs: selected_engine,
            inspector_factory=lambda _connection: FakeInspector(existing_tables),
        )
        return passed, "\n".join(output), selected_engine

    def test_aura_telegram_uat_is_accepted(self):
        passed, output, engine = self.run_database_check("aura_telegram_uat")

        self.assertTrue(passed)
        self.assertIn("current_database() is exactly aura_telegram_uat", output)
        self.assertIn("Database current_user: aura_uat_user", output)
        self.assertTrue(engine.disposed)

    def test_aura_is_rejected(self):
        passed, output, _engine = self.run_database_check("aura")

        self.assertFalse(passed)
        self.assertIn("FAIL: Database rejected.", output)

    def test_aura_test_is_rejected(self):
        passed, output, _engine = self.run_database_check("aura_test")

        self.assertFalse(passed)
        self.assertIn("FAIL: Database rejected.", output)

    def test_missing_database_url_is_rejected(self):
        passed, output, _engine = self.run_database_check(
            "aura_telegram_uat",
            settings_loader=lambda: SimpleNamespace(DATABASE_URL=None),
        )

        self.assertFalse(passed)
        self.assertIn("FAIL: DATABASE_URL is not configured.", output)

    def test_database_connection_failure_is_safe(self):
        secret_url = (
            "postgresql+psycopg://secret-user:secret-password@localhost/"
            "aura_telegram_uat"
        )
        secret_error = "secret-password connection refused"
        failed_engine = FakeEngine(
            "aura_telegram_uat",
            connection_error=RuntimeError(secret_error),
        )

        passed, output, engine = self.run_database_check(
            "aura_telegram_uat",
            settings_loader=lambda: database_settings(secret_url),
            engine=failed_engine,
        )

        self.assertFalse(passed)
        self.assertIn("failed safely", output)
        self.assertNotIn(secret_url, output)
        self.assertNotIn("secret-password", output)
        self.assertTrue(engine.disposed)

    def test_missing_required_table_is_rejected(self):
        required = set(uat_preflight.required_table_names())
        required.remove("conversation_workflow_states")

        passed, output, _engine = self.run_database_check(
            "aura_telegram_uat",
            tables=required,
        )

        self.assertFalse(passed)
        self.assertIn(
            "FAIL: Required AURA table is missing: "
            "conversation_workflow_states.",
            output,
        )


class DependencyPreflightTests(unittest.TestCase):
    def test_missing_ollama_model_is_rejected(self):
        output = []

        passed = uat_preflight.check_ollama(
            output=output.append,
            settings_loader=ai_settings,
            tags_fetcher=lambda _url: {
                "models": [{"name": "another-model:latest"}]
            },
        )

        self.assertFalse(passed)
        self.assertIn(
            "FAIL: Required Ollama model is unavailable: qwen2.5:3b.",
            output,
        )

    def test_missing_telegram_configuration_is_rejected(self):
        output = []

        passed = uat_preflight.check_telegram_configuration(
            output=output.append,
            settings_loader=lambda: telegram_settings(TELEGRAM_BOT_TOKEN=None),
            validator=validate_runner_configuration,
        )

        self.assertFalse(passed)
        self.assertEqual(
            output,
            ["FAIL: Required Telegram configuration is missing or invalid."],
        )

    def test_successful_preflight_does_not_print_secrets(self):
        output = []
        database_url = (
            "postgresql+psycopg://uat-user:database-password@localhost/"
            "aura_telegram_uat"
        )
        token = VALID_TOKEN
        identity_secret = IDENTITY_SECRET

        result = uat_preflight.run_preflight(
            output=output.append,
            database_settings_loader=lambda: database_settings(database_url),
            engine_factory=lambda *_args, **_kwargs: FakeEngine(
                "aura_telegram_uat"
            ),
            inspector_factory=lambda _connection: FakeInspector(
                uat_preflight.required_table_names()
            ),
            ai_settings_loader=ai_settings,
            ollama_tags_fetcher=lambda _url: {
                "models": [{"name": "qwen2.5:3b"}]
            },
            telegram_settings_loader=lambda: telegram_settings(
                TELEGRAM_BOT_TOKEN=token,
                TELEGRAM_IDENTITY_SECRET=identity_secret,
            ),
            telegram_validator=validate_runner_configuration,
        )

        rendered = "\n".join(output)
        self.assertEqual(result, 0)
        self.assertNotIn(database_url, rendered)
        self.assertNotIn("database-password", rendered)
        self.assertNotIn(token, rendered)
        self.assertNotIn(identity_secret, rendered)


@unittest.skipUnless(os.name == "nt", "Windows launcher test")
class WindowsLauncherTests(unittest.TestCase):
    def test_launcher_does_not_start_bot_after_preflight_failure(self):
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="aura uat launcher ") as temp:
            temp_root = Path(temp)
            launcher = temp_root / "run_telegram_uat.bat"
            shutil.copy2(project_root / "run_telegram_uat.bat", launcher)

            fake_bin = temp_root / "fake bin"
            fake_bin.mkdir()
            call_log = temp_root / "python calls.log"
            (fake_bin / "python.bat").write_text(
                "@echo off\n"
                '>>"%AURA_UAT_TEST_LOG%" echo %*\n'
                'if /I "%~1"=="tools\\uat_preflight.py" exit /b 17\n'
                "exit /b 0\n",
                encoding="utf-8",
            )

            environment = dict(os.environ)
            environment["VIRTUAL_ENV"] = str(temp_root / ".venv")
            environment["AURA_UAT_TEST_LOG"] = str(call_log)
            environment["PATH"] = str(fake_bin) + os.pathsep + environment["PATH"]

            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "call", str(launcher)],
                cwd=temp_root,
                env=environment,
                input="\n",
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

            calls = call_log.read_text(encoding="utf-8")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("tools\\uat_preflight.py", calls)
            self.assertNotIn("app.integrations.telegram.runner", calls)
            self.assertIn("The bot was not started.", result.stdout)


if __name__ == "__main__":
    unittest.main()
