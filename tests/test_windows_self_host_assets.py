"""Static safety checks for gated Windows deployment assets."""

from pathlib import Path
import re
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WINDOWS_ROOT = PROJECT_ROOT / "deploy" / "windows"
EXPECTED_SCRIPTS = {
    "Start-Aura.ps1",
    "Stop-Aura.ps1",
    "Test-AuraReadiness.ps1",
    "Run-DemoCleanup.ps1",
    "Backup-DemoDatabase.ps1",
    "Restore-DemoDatabase-Test.ps1",
    "Register-AuraTasks.ps1",
    "Unregister-AuraTasks.ps1",
    "Install-AuraFirewallRules.ps1",
    "Remove-AuraFirewallRules.ps1",
    "Test-LocalHostSecurity.ps1",
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
            "trycloudflare.com",
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

    def test_task_and_firewall_contracts_are_fixed(self):
        tasks = (WINDOWS_ROOT / "Register-AuraTasks.ps1").read_text(encoding="utf-8")
        firewall = (WINDOWS_ROOT / "Install-AuraFirewallRules.ps1").read_text(encoding="utf-8")
        self.assertIn("MultipleInstances IgnoreNew", tasks)
        self.assertIn("-Profile production -Foreground", tasks)
        self.assertEqual(tasks.count("-RestartCount"), 1)
        self.assertIn("'00:17'", tasks)
        for port in (8000, 8001, 5432):
            self.assertIn(f"-LocalPort {port}", firewall)
        self.assertNotIn("-Action Allow -Protocol TCP -LocalPort", firewall)

    def test_bootstrap_is_additive_and_non_superuser(self):
        sql = (WINDOWS_ROOT / "Bootstrap-LocalPostgreSQL.sql").read_text(
            encoding="utf-8"
        )
        self.assertIn("\\prompt -s", sql)
        self.assertIn("NOSUPERUSER", sql)
        self.assertNotRegex(
            sql.casefold(),
            r"(?m)^\s*(drop|truncate|delete)\b",
        )
        for database in ("aura_test", "aura_demo_staging", "aura_demo_public"):
            self.assertIn(database, sql)


if __name__ == "__main__":
    unittest.main()
