"""Offline regression coverage for the guarded local PostgreSQL test gate."""

from __future__ import annotations

from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from sqlalchemy.engine import URL, make_url

from app.core.config_validation import ConfigurationError, validate_app_environment
from tools.postgresql_test_preflight import (
    PostgreSQLTestPreflightError,
    build_test_database_url,
    validate_test_database_url,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ROOT = PROJECT_ROOT / "deploy" / "windows"
FIXED_URL = (
    "postgresql+psycopg://aura_test_runner@127.0.0.1:5432/aura_test"
)


class PostgreSQLTestURLTests(unittest.TestCase):
    def test_fixed_url_is_password_free_and_exact(self):
        built = build_test_database_url()
        self.assertEqual(built.render_as_string(hide_password=False), FIXED_URL)
        self.assertIsNone(built.password)
        self.assertEqual(validate_test_database_url(FIXED_URL), built)

    def test_wrong_or_credential_bearing_targets_are_rejected(self):
        invalid = (
            "postgresql+psycopg://aura_test_runner:secret@127.0.0.1:5432/aura_test",
            "postgresql+psycopg://postgres@127.0.0.1:5432/aura_test",
            "postgresql+psycopg://aura_test_runner@db.internal:5432/aura_test",
            "postgresql+psycopg://aura_test_runner@127.0.0.1:5432/postgres",
            "postgresql+psycopg://aura_test_runner@127.0.0.1:5432/aura_demo_staging",
            "postgresql+psycopg://aura_test_runner@127.0.0.1:5432/aura_demo_public",
            "sqlite:///aura_test",
            FIXED_URL + "?sslmode=require",
        )
        for value in invalid:
            with self.subTest(target=value.rsplit("/", 1)[-1]):
                with self.assertRaises(PostgreSQLTestPreflightError):
                    validate_test_database_url(value)

    def test_url_create_round_trips_reserved_password_characters(self):
        password = "Synthetic@Run:ner/Pass?With#Percent%And&Amp"
        built = URL.create(
            "postgresql+psycopg",
            username="aura_test_runner",
            password=password,
            host="127.0.0.1",
            port=5432,
            database="aura_test",
        )
        rendered = built.render_as_string(hide_password=False)
        self.assertEqual(make_url(rendered).password, password)
        self.assertNotIn(password, built.render_as_string())

    def test_invalid_url_error_never_contains_input(self):
        marker = "Never-Disclose-Database-Marker"
        with self.assertRaises(PostgreSQLTestPreflightError) as raised:
            validate_test_database_url(f"not-a-url-{marker}")
        self.assertNotIn(marker, str(raised.exception))


class PostgreSQLPowerShellRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = (
            WINDOWS_ROOT / "Run-AuraPostgreSQLTests.ps1"
        ).read_text(encoding="utf-8")
        cls.initializer = (
            WINDOWS_ROOT / "Initialize-AuraPostgreSQLTestCredential.ps1"
        ).read_text(encoding="utf-8")
        cls.bootstrap = (
            WINDOWS_ROOT / "Bootstrap-LocalPostgreSQL.sql"
        ).read_text(encoding="utf-8")
        cls.common = (
            WINDOWS_ROOT / "AuraWindows.Common.ps1"
        ).read_text(encoding="utf-8")
        cls.preflight = (
            PROJECT_ROOT / "tools" / "postgresql_test_preflight.py"
        ).read_text(encoding="utf-8")

    def test_runner_uses_child_only_explicit_test_environment(self):
        self.assertIn("[System.Diagnostics.ProcessStartInfo]::new()", self.runner)
        self.assertIn("EnvironmentVariables['APP_ENV'] = 'test'", self.runner)
        self.assertIn("EnvironmentVariables['AURA_DISABLE_DOTENV'] = '1'", self.runner)
        self.assertNotIn("$env:", self.runner)
        self.assertIn("EnvironmentVariables.Remove($name)", self.runner)
        self.assertIn("'OPENAI_API_KEY'", self.runner)
        self.assertIn("'TELEGRAM_BOT_TOKEN'", self.runner)

    def test_runner_uses_fixed_pgpass_and_unittest_contract(self):
        self.assertIn("'test.pgpass'", self.runner)
        self.assertIn("Assert-AuraOperatorSecretAcl", self.runner)
        self.assertIn(FIXED_URL, self.runner)
        self.assertNotIn("Read-Host", self.runner)
        self.assertIn(".StartsWith(\n            'PG'", self.runner)
        self.assertNotIn("PGPASSWORD", self.runner)
        self.assertIn(
            "-m unittest discover -s tests -p \"test_*.py\" -v",
            self.runner,
        )
        self.assertIn("tests.integration.test_public_reservation_api_postgresql", self.runner)

    def test_initializer_prompts_securely_and_escapes_pgpass(self):
        self.assertIn("Read-Host", self.initializer)
        self.assertIn("-AsSecureString", self.initializer)
        self.assertIn("SecureStringToBSTR", self.initializer)
        self.assertIn("ZeroFreeBSTR", self.initializer)
        self.assertIn("Replace('\\', '\\\\').Replace(':', '\\:')", self.initializer)
        self.assertIn("'test.pgpass'", self.initializer)
        self.assertIn("'/inheritance:r'", self.initializer)
        self.assertIn(".StartsWith(\n            'PG'", self.initializer)
        self.assertNotIn("PGPASSWORD", self.initializer)

    def test_initializer_requires_explicit_existing_credential_rotation(self):
        self.assertIn("param([switch]$ReplaceExisting)", self.initializer)
        self.assertIn(
            "if ($credentialExists -and -not $ReplaceExisting)",
            self.initializer,
        )
        self.assertIn("AURA_TEST_PGPASSFILE_ALREADY_EXISTS", self.initializer)
        self.assertIn(
            "if (-not $credentialExists -and $ReplaceExisting)",
            self.initializer,
        )
        self.assertIn(
            "AURA_TEST_PGPASSFILE_MISSING_FOR_REPLACEMENT",
            self.initializer,
        )

    def test_rotation_validates_temporary_credential_before_atomic_replace(self):
        write = self.initializer.index("[IO.File]::WriteAllText(")
        temporary_acl = self.initializer.index(
            "Set-AuraTestCredentialAcl -Path $tempPath"
        )
        validation = self.initializer.index(
            "Test-AuraTestCredential -CredentialPath $tempPath"
        )
        replacement = self.initializer.index(
            "Replace-AuraFileWithoutBackup"
            " `\n            -SourcePath $tempPath -DestinationPath $pgPassPath",
            validation,
        )
        final_acl = self.initializer.index(
            "Assert-AuraOperatorSecretAcl -Path $pgPassPath",
            replacement,
        )
        self.assertLess(write, temporary_acl)
        self.assertLess(temporary_acl, validation)
        self.assertLess(validation, replacement)
        self.assertLess(replacement, final_acl)
        self.assertIn("AURA_TEST_CREDENTIAL_VALIDATION_FAILED", self.initializer)
        self.assertIn(
            "Remove-Item -LiteralPath $tempPath -Force",
            self.initializer,
        )

    def test_windows_powershell_null_binding_regression_and_real_replace(self):
        if os.name != "nt":
            self.skipTest("Windows File.Replace regression")
        with tempfile.TemporaryDirectory(prefix="aura-file-replace-") as root:
            source = Path(root) / "source.tmp"
            destination = Path(root) / "destination.txt"
            source.write_text("new-dummy-credential", encoding="utf-8")
            destination.write_text("old-dummy-credential", encoding="utf-8")
            script = (
                "$ErrorActionPreference='Stop';"
                "$source=$env:AURA_DUMMY_REPLACE_SOURCE;"
                "$destination=$env:AURA_DUMMY_REPLACE_DESTINATION;"
                "try{[IO.File]::Replace($source,$destination,$null);"
                "Write-Output 'DIRECT_UNEXPECTED_SUCCESS';exit 9}"
                "catch{if($_.Exception.InnerException -isnot [ArgumentException])"
                "{Write-Output 'DIRECT_WRONG_ERROR';exit 8};"
                "Write-Output 'DIRECT_EMPTY_BACKUP_REPRODUCED'};"
                "$method=[IO.File].GetMethod('Replace',"
                "[Type[]]@([string],[string],[string]));"
                "[void]$method.Invoke($null,[object[]]@($source,$destination,$null));"
                "Write-Output 'TRUE_NULL_REPLACE_OK'"
            )
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    script,
                ],
                cwd=PROJECT_ROOT,
                env={
                    **os.environ,
                    "AURA_DUMMY_REPLACE_SOURCE": str(source),
                    "AURA_DUMMY_REPLACE_DESTINATION": str(destination),
                },
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("DIRECT_EMPTY_BACKUP_REPRODUCED", result.stdout)
            self.assertIn("TRUE_NULL_REPLACE_OK", result.stdout)
            self.assertFalse(source.exists())
            self.assertEqual(
                destination.read_text(encoding="utf-8-sig"),
                "new-dummy-credential",
            )

    def test_initializer_passes_true_null_to_no_backup_overload(self):
        self.assertNotIn(
            "[IO.File]::Replace($tempPath, $pgPassPath, $null)",
            self.initializer,
        )
        self.assertIn(
            "[Type[]]@([string], [string], [string])",
            self.initializer,
        )
        self.assertIn(
            "[object[]]@($SourcePath, $DestinationPath, $null)",
            self.initializer,
        )
        self.assertNotIn("destinationBackupFileName", self.initializer)

    def test_rotation_validation_uses_passwordless_child_process(self):
        validation_function = self.initializer.split(
            "function Test-AuraTestCredential", 1
        )[1].split("$tempName", 1)[0]
        self.assertIn("RedirectStandardOutput = $true", self.initializer)
        self.assertIn("RedirectStandardError = $true", self.initializer)
        self.assertIn("Arguments = '-B -m tools.postgresql_test_preflight'", self.initializer)
        self.assertIn("EnvironmentVariables['PGPASSFILE'] = $CredentialPath", self.initializer)
        self.assertIn("EnvironmentVariables.Remove($name)", validation_function)
        self.assertIn("'OPENAI_API_KEY'", validation_function)
        self.assertIn("'TELEGRAM_BOT_TOKEN'", validation_function)
        self.assertIn(".StartsWith(\n            'PG'", validation_function)
        self.assertIn(FIXED_URL, self.initializer)
        self.assertNotIn("$plainPassword", validation_function)
        self.assertNotIn("$escapedPassword", validation_function)
        self.assertNotIn("Write-Output $standardOutput", self.initializer)
        self.assertIn("AURA_TEST_PGPASSFILE_UPDATED", self.initializer)

    def test_credential_acl_rejects_broad_and_creator_principals(self):
        for sid in ("S-1-1-0", "S-1-3-0", "S-1-5-11", "S-1-5-32-545"):
            self.assertIn(sid, self.common)
        self.assertIn("AURA_SECRET_ACL_UNEXPECTED_IDENTITY", self.common)

    def test_runner_orders_preflight_focused_then_full(self):
        preflight = self.runner.index("tools.postgresql_test_preflight")
        focused = self.runner.index(
            "tests.integration.test_public_reservation_api_postgresql"
        )
        full = self.runner.index(
            '-m unittest discover -s tests -p "test_*.py" -v'
        )
        self.assertLess(preflight, focused)
        self.assertLess(focused, full)
        self.assertIn("if ($focusedExitCode -ne 0)", self.runner)
        self.assertIn("PGPASSFILE present: yes", self.runner)
        self.assertIn("credential file ACL protected: yes", self.runner)

    def test_bootstrap_matches_suite_role_and_minimum_schema_capability(self):
        self.assertIn("aura_test_runner", self.bootstrap)
        self.assertNotIn("GRANT CONNECT, TEMPORARY ON DATABASE aura_test TO aura_test_runtime", self.bootstrap)
        self.assertIn(
            "GRANT CONNECT, CREATE ON DATABASE aura_test TO aura_test_runner",
            self.bootstrap,
        )
        self.assertIn("ALTER DATABASE aura_test OWNER TO aura_migration_owner", self.bootstrap)
        self.assertIn("REVOKE CREATE ON SCHEMA public FROM aura_test_runner", self.bootstrap)
        self.assertIn("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM aura_test_runner", self.bootstrap)
        self.assertIn("NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS", self.bootstrap)

    def test_bootstrap_uses_postgresql_18_secure_password_commands(self):
        self.assertIn("\\set ON_ERROR_STOP on", self.bootstrap)
        self.assertIn("\\set ECHO none", self.bootstrap)
        self.assertIn("\\set VERBOSITY terse", self.bootstrap)
        self.assertNotIn("\\prompt -s", self.bootstrap)
        self.assertNotIn("\\prompt", self.bootstrap)
        self.assertNotRegex(self.bootstrap, r":'[A-Za-z_][A-Za-z0-9_]*password")
        self.assertNotIn("PASSWORD %L", self.bootstrap)
        for role in (
            "aura_migration_owner",
            "aura_test_runner",
            "aura_staging_runtime",
            "aura_public_runtime",
        ):
            with self.subTest(role=role):
                secure_prompt = f"\\password {role}"
                create_no_login = f"CREATE ROLE {role} NOLOGIN"
                alter_login = f"ALTER ROLE {role} LOGIN"
                self.assertEqual(self.bootstrap.count(secure_prompt), 1)
                self.assertIn(create_no_login, self.bootstrap)
                self.assertIn(alter_login, self.bootstrap)
                self.assertLess(
                    self.bootstrap.index(create_no_login),
                    self.bootstrap.index(secure_prompt),
                )
                self.assertLess(
                    self.bootstrap.index(secure_prompt),
                    self.bootstrap.index(alter_login),
                )

    def test_bootstrap_prompts_only_for_missing_passwords(self):
        self.assertEqual(self.bootstrap.count("rolpassword IS NULL"), 4)
        self.assertEqual(self.bootstrap.count("FROM pg_authid WHERE rolname"), 4)
        for variable in (
            "migration_owner_password_missing",
            "test_runner_password_missing",
            "staging_runtime_password_missing",
            "public_runtime_password_missing",
        ):
            self.assertIn(f"\\if :{variable}", self.bootstrap)

    def test_bootstrap_target_guard_precedes_every_mutation(self):
        guard = self.bootstrap.index("\\if :target_valid")
        invalid_exit = self.bootstrap.index("\\quit 3", guard)
        first_role_mutation = self.bootstrap.index("CREATE ROLE")
        self.assertLess(guard, invalid_exit)
        self.assertLess(invalid_exit, first_role_mutation)
        for target in ("test", "staging", "production"):
            self.assertIn(f":'target' = '{target}'", self.bootstrap)

    def test_test_runner_receives_no_demo_database_grant(self):
        for database in ("aura_demo_staging", "aura_demo_public"):
            self.assertNotRegex(
                self.bootstrap,
                rf"GRANT[^;]*ON DATABASE {database}[^;]*TO aura_test_runner",
            )

    def test_preflight_checks_identity_privileges_and_cross_database_access(self):
        for required in (
            "current_database(), current_user",
            "rolsuper",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
            "pg_auth_members",
            "aura_demo_public",
            "aura_demo_staging",
            "AURA_TEST_DATABASE_OWNER_INVALID",
        ):
            self.assertIn(required, self.preflight)


class AppEnvironmentRegressionTests(unittest.TestCase):
    def test_missing_and_invalid_app_env_still_fail_closed(self):
        for value in (None, "", "Test", "prod"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigurationError):
                    validate_app_environment(value)

    def test_test_demo_and_production_remain_exact(self):
        for value in ("test", "demo", "production"):
            with self.subTest(value=value):
                self.assertEqual(validate_app_environment(value), value)


if __name__ == "__main__":
    unittest.main()
