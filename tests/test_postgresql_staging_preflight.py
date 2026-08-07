"""Regression coverage for secret-safe Windows staging provisioning."""

from __future__ import annotations

from pathlib import Path
import unittest

from tools.postgresql_staging_preflight import (
    PostgreSQLStagingPreflightError,
    build_staging_database_url,
    validate_staging_database_url,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ROOT = PROJECT_ROOT / "deploy" / "windows"
FIXED_URL = (
    "postgresql+psycopg://aura_staging_runtime@127.0.0.1:5432/"
    "aura_demo_staging"
)


class PostgreSQLStagingURLTests(unittest.TestCase):
    def test_fixed_url_is_password_free_and_exact(self):
        built = build_staging_database_url()
        self.assertEqual(built.render_as_string(hide_password=False), FIXED_URL)
        self.assertIsNone(built.password)
        self.assertEqual(validate_staging_database_url(FIXED_URL), built)

    def test_wrong_or_credential_bearing_targets_are_rejected(self):
        invalid = (
            "postgresql+psycopg://aura_staging_runtime:secret@127.0.0.1:5432/aura_demo_staging",
            "postgresql+psycopg://postgres@127.0.0.1:5432/aura_demo_staging",
            "postgresql+psycopg://aura_staging_runtime@db.internal:5432/aura_demo_staging",
            "postgresql+psycopg://aura_staging_runtime@127.0.0.1:5432/aura_test",
            "postgresql+psycopg://aura_staging_runtime@127.0.0.1:5432/aura_demo_public",
            "sqlite:///aura_demo_staging",
            FIXED_URL + "?sslmode=require",
        )
        for value in invalid:
            with self.subTest(target=value.rsplit("/", 1)[-1]):
                with self.assertRaises(PostgreSQLStagingPreflightError):
                    validate_staging_database_url(value)

    def test_invalid_url_error_never_contains_input(self):
        marker = "Never-Disclose-Staging-Database-Marker"
        with self.assertRaises(PostgreSQLStagingPreflightError) as raised:
            validate_staging_database_url(f"not-a-url-{marker}")
        self.assertNotIn(marker, str(raised.exception))


class PostgreSQLStagingInitializerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.initializer = (
            WINDOWS_ROOT / "Initialize-AuraPostgreSQLStagingCredential.ps1"
        ).read_text(encoding="utf-8")
        cls.preflight = (
            PROJECT_ROOT / "tools" / "postgresql_staging_preflight.py"
        ).read_text(encoding="utf-8")

    def test_initializer_has_fixed_staging_only_contract(self):
        self.assertIn("'staging.pgpass'", self.initializer)
        self.assertIn("Import-AuraConfiguration -Profile staging", self.initializer)
        self.assertIn(FIXED_URL, self.initializer)
        self.assertIn("aura_staging_runtime", self.initializer)
        self.assertIn("aura_demo_staging", self.initializer)
        self.assertNotIn("aura_demo_public:", self.initializer)
        self.assertNotIn("test.pgpass", self.initializer)

    def test_initializer_prompts_and_handles_secret_without_output(self):
        self.assertIn("Read-Host", self.initializer)
        self.assertIn("-AsSecureString", self.initializer)
        self.assertIn("SecureStringToBSTR", self.initializer)
        self.assertIn("ZeroFreeBSTR", self.initializer)
        self.assertIn("Replace('\\', '\\\\').Replace(':', '\\:')", self.initializer)
        self.assertNotIn("PGPASSWORD", self.initializer)
        self.assertNotIn("Write-Output $standardOutput", self.initializer)
        self.assertNotIn("Write-Output $plainPassword", self.initializer)

    def test_temporary_file_is_protected_validated_and_atomically_installed(self):
        write = self.initializer.index("[IO.File]::WriteAllText(")
        acl = self.initializer.index("Set-AuraStagingCredentialAcl -Path $tempPath")
        validate = self.initializer.index(
            "Test-AuraStagingCredential -CredentialPath $tempPath"
        )
        move = self.initializer.index("[IO.File]::Move($tempPath, $pgPassPath)")
        self.assertLess(write, acl)
        self.assertLess(acl, validate)
        self.assertLess(validate, move)
        self.assertIn("[object[]]@($SourcePath, $DestinationPath, $null)", self.initializer)
        self.assertNotIn("destinationBackupFileName", self.initializer)
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
            "aura_demo_public",
            "AURA_POSTGRESQL_STAGING_PREFLIGHT_OK",
        ):
            self.assertIn(expected, self.preflight)


if __name__ == "__main__":
    unittest.main()
