"""Regression coverage for secret-safe Windows production provisioning."""

from __future__ import annotations

from pathlib import Path
import unittest

from tools.postgresql_production_preflight import (
    PostgreSQLProductionPreflightError,
    build_production_database_url,
    validate_production_database_url,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ROOT = PROJECT_ROOT / "deploy" / "windows"
FIXED_URL = (
    "postgresql+psycopg://aura_public_runtime@127.0.0.1:5432/"
    "aura_demo_public"
)


class PostgreSQLProductionURLTests(unittest.TestCase):
    def test_fixed_url_is_password_free_and_exact(self):
        built = build_production_database_url()
        self.assertEqual(built.render_as_string(hide_password=False), FIXED_URL)
        self.assertIsNone(built.password)
        self.assertEqual(validate_production_database_url(FIXED_URL), built)

    def test_wrong_or_credential_bearing_targets_are_rejected(self):
        invalid = (
            "postgresql+psycopg://aura_public_runtime:secret@127.0.0.1:5432/aura_demo_public",
            "postgresql+psycopg://postgres@127.0.0.1:5432/aura_demo_public",
            "postgresql+psycopg://aura_public_runtime@db.internal:5432/aura_demo_public",
            "postgresql+psycopg://aura_public_runtime@127.0.0.1:5432/aura_test",
            "postgresql+psycopg://aura_public_runtime@127.0.0.1:5432/aura_demo_staging",
            "sqlite:///aura_demo_public",
            FIXED_URL + "?sslmode=require",
        )
        for value in invalid:
            with self.subTest(target=value.rsplit("/", 1)[-1]):
                with self.assertRaises(PostgreSQLProductionPreflightError):
                    validate_production_database_url(value)

    def test_invalid_url_error_never_contains_input(self):
        marker = "Never-Disclose-Production-Database-Marker"
        with self.assertRaises(PostgreSQLProductionPreflightError) as raised:
            validate_production_database_url(f"not-a-url-{marker}")
        self.assertNotIn(marker, str(raised.exception))


class PostgreSQLProductionInitializerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.initializer = (
            WINDOWS_ROOT / "Initialize-AuraPostgreSQLProductionCredential.ps1"
        ).read_text(encoding="utf-8")
        cls.preflight = (
            PROJECT_ROOT / "tools" / "postgresql_production_preflight.py"
        ).read_text(encoding="utf-8")

    def test_initializer_has_fixed_production_only_contract(self):
        self.assertIn("'production.pgpass'", self.initializer)
        self.assertIn("Import-AuraConfiguration -Profile production", self.initializer)
        self.assertIn(FIXED_URL, self.initializer)
        self.assertIn("aura_public_runtime", self.initializer)
        self.assertIn("aura_demo_public", self.initializer)
        self.assertNotIn("aura_demo_staging:", self.initializer)
        self.assertNotIn("test.pgpass", self.initializer)

    def test_initializer_prompts_and_handles_secret_without_output(self):
        for expected in (
            "Read-Host",
            "-AsSecureString",
            "SecureStringToBSTR",
            "ZeroFreeBSTR",
            "Replace('\\', '\\\\').Replace(':', '\\:')",
        ):
            self.assertIn(expected, self.initializer)
        self.assertNotIn("PGPASSWORD", self.initializer)
        self.assertNotIn("Write-Output $standardOutput", self.initializer)
        self.assertNotIn("Write-Output $plainPassword", self.initializer)

    def test_temporary_file_is_protected_validated_and_atomically_installed(self):
        write = self.initializer.index("[IO.File]::WriteAllText(")
        acl = self.initializer.index("Set-AuraProductionCredentialAcl -Path $tempPath")
        validate = self.initializer.index(
            "Test-AuraProductionCredential -CredentialPath $tempPath"
        )
        move = self.initializer.index("[IO.File]::Move($tempPath, $pgPassPath)")
        self.assertLess(write, acl)
        self.assertLess(acl, validate)
        self.assertLess(validate, move)
        self.assertIn(
            "[object[]]@($SourcePath, $DestinationPath, $null)",
            self.initializer,
        )
        self.assertIn("Remove-Item -LiteralPath $tempPath -Force", self.initializer)

    def test_preflight_enforces_least_privilege_and_cross_database_denial(self):
        for expected in (
            "rolsuper",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
            "rolbypassrls",
            "TEMPORARY",
            "TRUNCATE,REFERENCES,TRIGGER",
            "aura_test",
            "aura_demo_staging",
            "AURA_POSTGRESQL_PRODUCTION_PREFLIGHT_OK",
        ):
            self.assertIn(expected, self.preflight)


class ProductionRestoreAssetTests(unittest.TestCase):
    def test_restore_uses_temporary_migration_credential_and_restore_test_only(self):
        script = (WINDOWS_ROOT / "Restore-DemoDatabase-Test.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("$targetDatabase = 'aura_restore_test'", script)
        self.assertIn("Read-Host", script)
        self.assertIn("-AsSecureString", script)
        self.assertIn("restore-migration.pgpass.", script)
        self.assertIn("ConvertFrom-AuraSchemaProcessResult", script)
        self.assertIn("-Operation verify", script)
        self.assertIn("actualTableCount -ne 10", script)
        self.assertNotIn("Import-AuraConfiguration -Profile 'staging'", script)
        self.assertNotIn("PGPASSWORD", script)
        self.assertIn("Remove-Item -LiteralPath $tempPath -Force", script)

    def test_restore_requires_source_profile_filename_and_explicit_drop(self):
        script = (WINDOWS_ROOT / "Restore-DemoDatabase-Test.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("$expectedSourceDatabase", script)
        self.assertIn(
            "[Parameter(Mandatory)][ValidateSet('staging', 'production')]",
            script,
        )
        self.assertIn("RESTORE_TO_AURA_RESTORE_TEST", script)
        self.assertIn("DROP_AURA_RESTORE_TEST", script)
        self.assertIn("AURA_RESTORE_TARGET_NOT_EMPTY", script)

    def test_restore_failure_reports_exact_safe_stage(self):
        script = (WINDOWS_ROOT / "Restore-DemoDatabase-Test.ps1").read_text(
            encoding="utf-8"
        )
        stages = (
            "AURA_RESTORE_CREDENTIAL_STAGE_FAILED",
            "AURA_RESTORE_ARCHIVE_VALIDATION_STAGE_FAILED",
            "AURA_RESTORE_TARGET_PREFLIGHT_STAGE_FAILED",
            "AURA_RESTORE_DATABASE_CREATE_STAGE_FAILED",
            "AURA_RESTORE_PG_RESTORE_STAGE_FAILED",
            "AURA_RESTORE_SCHEMA_VERIFICATION_STAGE_FAILED",
            "AURA_RESTORE_AGGREGATE_VERIFICATION_STAGE_FAILED",
            "AURA_RESTORE_DROP_STAGE_FAILED",
        )
        for stage in stages:
            self.assertIn(stage, script)
        self.assertIn("throw $failureCode", script)
        self.assertNotIn("throw 'AURA_RESTORE_FAILED'", script)


class ExistingRestoreVerifierAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (
            WINDOWS_ROOT / "Test-AuraRestoredDatabase.ps1"
        ).read_text(encoding="utf-8")

    def test_verifier_is_exact_target_and_read_only(self):
        for expected in (
            "VERIFY_EXISTING_AURA_RESTORE_TEST",
            "$targetDatabase = 'aura_restore_test'",
            "default_transaction_read_only=on",
            "--set=ON_ERROR_STOP=1",
            "ConvertFrom-AuraSchemaProcessResult",
            "matchingColumnCount",
            "matchingPrimaryKeyCount",
            "matchingTableStructureCount",
            "aggregateRowEstimate",
            "readOnly=true",
            ") -f `",
        ):
            self.assertIn(expected, self.script)
        for forbidden in (
            "createdb.exe",
            "pg_restore.exe",
            "dropdb.exe",
            "aura_demo_public@",
            "aura_demo_staging@",
            "aura_test@",
            "Base.metadata.create_all",
        ):
            self.assertNotIn(forbidden, self.script)

    def test_verifier_handles_secret_locally_and_always_cleans_up(self):
        for expected in (
            "Read-Host",
            "-AsSecureString",
            "SecureStringToBSTR",
            "ZeroFreeBSTR",
            "Set-AuraOperatorProtectedAcl -Path $tempPath",
            "Remove-Item -LiteralPath $tempPath -Force",
            "AURA_RESTORE_EXISTING_SCHEMA_VERIFICATION_STAGE_FAILED",
            "AURA_RESTORE_EXISTING_AGGREGATE_VERIFICATION_STAGE_FAILED",
        ):
            self.assertIn(expected, self.script)
        self.assertNotIn("PGPASSWORD", self.script)
        self.assertNotIn("Write-Output $standardError", self.script)
        self.assertNotIn("Write-Output $plainPassword", self.script)


if __name__ == "__main__":
    unittest.main()
