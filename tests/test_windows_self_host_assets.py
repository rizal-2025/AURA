"""Static safety checks for gated Windows deployment assets."""

from pathlib import Path
import re
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ROOT = PROJECT_ROOT / "deploy" / "windows"
EXPECTED_SCRIPTS = {
    "Start-Aura.ps1",
    "Stop-Aura.ps1",
    "Start-AuraPublicDemo.ps1",
    "Stop-AuraPublicDemo.ps1",
    "Start-TailscaleFunnel.ps1",
    "Stop-TailscaleFunnel.ps1",
    "Test-TailscaleFunnel.ps1",
    "Test-PublicDemoReadiness.ps1",
    "Test-AuraReadiness.ps1",
    "Run-DemoCleanup.ps1",
    "Backup-DemoDatabase.ps1",
    "Restore-DemoDatabase-Test.ps1",
    "Register-AuraTasks.ps1",
    "Unregister-AuraTasks.ps1",
    "Install-AuraFirewallRules.ps1",
    "Remove-AuraFirewallRules.ps1",
    "Test-LocalHostSecurity.ps1",
    "Run-AuraPostgreSQLTests.ps1",
    "Initialize-AuraPostgreSQLTestCredential.ps1",
    "Initialize-AuraPostgreSQLStagingCredential.ps1",
    "Initialize-AuraPostgreSQLStagingSchema.ps1",
    "Initialize-AuraPostgreSQLProductionCredential.ps1",
    "Initialize-AuraPostgreSQLProductionSchema.ps1",
}


class WindowsSelfHostAssetTests(unittest.TestCase):
    def test_expected_value_free_assets_exist(self):
        for name in EXPECTED_SCRIPTS | {
            "AuraWindows.Common.ps1",
            "secrets.template.conf",
            "Bootstrap-LocalPostgreSQL.sql",
            "README.md",
        }:
            with self.subTest(name=name):
                self.assertTrue((WINDOWS_ROOT / name).is_file())

    def test_powershell_avoids_dynamic_execution_and_embedded_credentials(self):
        combined = "\n".join(
            (WINDOWS_ROOT / name).read_text(encoding="utf-8")
            for name in EXPECTED_SCRIPTS | {"AuraWindows.Common.ps1"}
        )
        for forbidden in (
            "Invoke-Expression",
            "ScriptBlock]::Create",
            "ConvertTo-SecureString -AsPlainText",
            "--password",
            "PGPASSWORD",
            "0.0.0.0",
        ):
            self.assertNotIn(forbidden.casefold(), combined.casefold())
        self.assertIn("PGPASSFILE", combined)
        self.assertIn("127.0.0.1", combined)

    def test_secret_template_contains_names_only(self):
        template = (WINDOWS_ROOT / "secrets.template.conf").read_text(
            encoding="utf-8"
        )
        assignments = [line for line in template.splitlines() if line and not line.startswith("#")]
        self.assertGreater(len(assignments), 10)
        for assignment in assignments:
            self.assertRegex(assignment, r"^[A-Z][A-Z0-9_]*=$")

    def test_backup_and_restore_are_allowlisted_and_confirmed(self):
        backup = (WINDOWS_ROOT / "Backup-DemoDatabase.ps1").read_text(encoding="utf-8")
        restore = (WINDOWS_ROOT / "Restore-DemoDatabase-Test.ps1").read_text(encoding="utf-8")
        self.assertIn("Assert-AuraPathWithin", backup + restore)
        self.assertIn("RESTORE_TO_AURA_RESTORE_TEST", restore)
        self.assertIn("DROP_AURA_RESTORE_TEST", restore)
        self.assertNotRegex(backup, re.compile(r"--dbname=\$env:DEMO_DATABASE_URL", re.I))
        self.assertIn('-Filter "${expectedDatabase}_*.dump"', backup)
        self.assertNotIn("-Filter 'aura_demo_*.dump'", backup)

    def test_task_and_firewall_contracts_are_fixed(self):
        tasks = (WINDOWS_ROOT / "Register-AuraTasks.ps1").read_text(encoding="utf-8")
        firewall = (WINDOWS_ROOT / "Install-AuraFirewallRules.ps1").read_text(encoding="utf-8")
        self.assertIn("MultipleInstances IgnoreNew", tasks)
        self.assertNotIn("AtLogOn", tasks)
        self.assertNotIn("AURA API Production", tasks)
        self.assertNotIn("Start-Aura", tasks)
        self.assertIn("'00:17'", tasks)
        for port in (8000, 8001, 5432):
            self.assertIn(f"-LocalPort {port}", firewall)
        self.assertNotIn("-Action Allow -Protocol TCP -LocalPort", firewall)

    def test_funnel_lifecycle_is_manual_exact_and_non_persistent(self):
        common = (WINDOWS_ROOT / "AuraWindows.Common.ps1").read_text(encoding="utf-8")
        start = (WINDOWS_ROOT / "Start-TailscaleFunnel.ps1").read_text(encoding="utf-8")
        orchestrator = (WINDOWS_ROOT / "Start-AuraPublicDemo.ps1").read_text(
            encoding="utf-8"
        )
        funnel_stop = (WINDOWS_ROOT / "Stop-TailscaleFunnel.ps1").read_text(
            encoding="utf-8"
        )
        stop = (WINDOWS_ROOT / "Stop-AuraPublicDemo.ps1").read_text(encoding="utf-8")
        self.assertIn("return 443", common)
        self.assertIn("return 8443", common)
        self.assertIn("http://127.0.0.1:$port", common)
        self.assertIn("funnel status --json", common)
        self.assertNotIn("--set-path", start)
        self.assertNotIn("'--bg'", start)
        self.assertNotIn('"--https=$publicPort" off', start + funnel_stop)
        self.assertIn("funnel reset", start)
        self.assertIn("funnel reset", funnel_stop)
        self.assertIn("AURA_FUNNEL_OTHER_PROFILE_ACTIVE", start)
        self.assertIn("AURA_FUNNEL_OTHER_PROFILE_ACTIVE", funnel_stop)
        self.assertNotIn("finally", stop)
        self.assertIn("$publicBoundaryInactive", orchestrator)
        self.assertIn("AURA_PUBLIC_DEMO_ROLLBACK_FAILED", orchestrator)
        self.assertLess(
            orchestrator.index("if ($auraStarted -and $publicBoundaryInactive)"),
            orchestrator.index("AURA_PUBLIC_DEMO_ROLLBACK_FAILED"),
        )
        self.assertLess(stop.index("Stop-TailscaleFunnel.ps1"), stop.index("Stop-Aura.ps1"))

    def test_tailscale_cli_discovery_accepts_only_signed_official_binary(self):
        common = (WINDOWS_ROOT / "AuraWindows.Common.ps1").read_text(
            encoding="utf-8"
        )
        fixed_install = common.index("'Tailscale\\tailscale.exe'")
        path_lookup = common.index("Get-Command tailscale.exe")
        self.assertLess(fixed_install, path_lookup)
        self.assertIn("[Environment+SpecialFolder]::ProgramFiles", common)
        self.assertIn("-CommandType Application", common)
        self.assertIn("-ErrorAction SilentlyContinue", common)
        self.assertIn("Get-AuthenticodeSignature -LiteralPath $path", common)
        self.assertIn("[System.Management.Automation.SignatureStatus]::Valid", common)
        self.assertIn("'CN=Tailscale Inc.,'", common)
        self.assertIn("AURA_TAILSCALE_NOT_FOUND", common)

    def test_bootstrap_is_additive_and_non_superuser(self):
        sql = (WINDOWS_ROOT / "Bootstrap-LocalPostgreSQL.sql").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("\\prompt", sql)
        self.assertIn("\\password aura_migration_owner", sql)
        self.assertIn("NOSUPERUSER", sql)
        self.assertNotRegex(
            sql.casefold(),
            r"(?m)^\s*(drop|truncate|delete)\b",
        )
        for database in ("aura_test", "aura_demo_staging", "aura_demo_public"):
            self.assertIn(database, sql)

    def test_staging_schema_initializer_is_empty_only_and_secret_safe(self):
        script = (
            WINDOWS_ROOT / "Initialize-AuraPostgreSQLStagingSchema.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("Read-Host", script)
        self.assertIn("-AsSecureString", script)
        self.assertIn("SecureStringToBSTR", script)
        self.assertIn("ZeroFreeBSTR", script)
        self.assertIn("additive-empty-schema", script)
        self.assertIn("actualTableCount -ne 0", script)
        self.assertLess(script.index("-Operation plan"), script.index("-Operation apply-empty-schema"))
        self.assertLess(script.index("-Operation apply-empty-schema"), script.index("-Operation verify"))
        self.assertIn("Remove-Item -LiteralPath $tempPath -Force", script)
        self.assertIn("ConvertFrom-AuraSchemaProcessResult", script)
        self.assertNotIn(
            "-or -not [string]::IsNullOrEmpty($standardError)",
            script,
        )
        self.assertNotIn("PGPASSWORD", script)
        self.assertNotIn("aura_demo_public", script)
        self.assertNotIn("test.pgpass", script)

    def test_production_schema_initializer_is_empty_only_and_secret_safe(self):
        script = (
            WINDOWS_ROOT / "Initialize-AuraPostgreSQLProductionSchema.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("Read-Host", script)
        self.assertIn("-AsSecureString", script)
        self.assertIn("SecureStringToBSTR", script)
        self.assertIn("ZeroFreeBSTR", script)
        self.assertIn("additive-empty-schema", script)
        self.assertIn("actualTableCount -ne 0", script)
        self.assertLess(script.index("-Operation plan"), script.index("-Operation apply-empty-schema"))
        self.assertLess(script.index("-Operation apply-empty-schema"), script.index("-Operation verify"))
        self.assertIn("Remove-Item -LiteralPath $tempPath -Force", script)
        self.assertIn("-Profile production", script)
        self.assertNotIn("PGPASSWORD", script)
        self.assertNotIn("aura_demo_staging", script)
        self.assertNotIn("aura_test", script)


if __name__ == "__main__":
    unittest.main()
