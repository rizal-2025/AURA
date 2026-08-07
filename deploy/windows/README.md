# AURA Windows public-demo operations

These value-free assets operate a manually started, loopback-only AURA Funnel
gateway with local PostgreSQL. Do not put secrets, the stable Funnel hostname,
Tailscale auth material, or live account identifiers in this repository.

## Fixed topology

| Profile | AURA listener | Funnel HTTPS |
|---|---|---:|
| staging | `127.0.0.1:8001` | 8443 |
| production | `127.0.0.1:8000` | 443 |

Funnel proxies `/` to the profile's exact `http://127.0.0.1` target. Route
exposure is controlled by `app.funnel_main`, which provides only health and the
five internal demo operations. The full AURA app is never the Funnel target.

## One-time local preparation

1. Install PostgreSQL locally and create the additive roles/databases with
   `Bootstrap-LocalPostgreSQL.sql`.
2. Copy `secrets.template.conf` outside Git to
   `C:\ProgramData\AURA\secrets\staging.conf` and/or `production.conf`.
3. Disable ACL inheritance and grant only SYSTEM, Administrators, and the
   protected operator account access to each secret file.
4. Install the three inbound block rules from an elevated shell:

   ```powershell
   .\Install-AuraFirewallRules.ps1 -Confirmation INSTALL_AURA_FIREWALL_RULES
   ```

5. Optionally register maintenance only:

   ```powershell
   .\Register-AuraTasks.ps1
   ```

   This registers hourly cleanup and daily backup. It does not register AURA
   or Funnel startup.

## Local PostgreSQL integration tests

The unittest PostgreSQL gate uses only `aura_test` with the dedicated
`aura_test_runner` login. `aura_migration_owner` owns the database, while the
runner receives only `CONNECT`, database-level `CREATE`, and public-schema
`USAGE` so it can own and remove its generated disposable schemas. It receives
no public-schema table or sequence access. The test process never needs the
migration-owner credential.

From an elevated PowerShell window at the repository root, provision or repair
the test-only roles and database. `psql` prompts locally and does not put a
password in command history:

```powershell
& 'C:\Program Files\PostgreSQL\18\bin\psql.exe' `
  --host=127.0.0.1 --port=5432 --username=postgres --dbname=postgres `
  --set=target=test --file=.\deploy\windows\Bootstrap-LocalPostgreSQL.sql
```

Then create the protected standard PostgreSQL password file. Enter the existing
`aura_test_runner` password only at the secure local prompt:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\deploy\windows\Initialize-AuraPostgreSQLTestCredential.ps1
```

The default command refuses to overwrite an existing credential. After an
intentional PostgreSQL password rotation, update it explicitly:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\deploy\windows\Initialize-AuraPostgreSQLTestCredential.ps1 -ReplaceExisting
```

The replacement is written to an ACL-protected temporary file in the same
directory, validated through the password-free Python/psycopg preflight, and
atomically installed only after authentication and identity checks pass. A bad
password leaves the existing credential unchanged and removes the temporary
file.

The credential is stored outside Git at
`C:\ProgramData\AURA\secrets\test.pgpass` with protected ACLs. Do not open,
print, copy, or commit it. Passwords containing PostgreSQL URI-reserved
characters are supported because the test URL contains no password; libpq reads
the escaped password from `PGPASSFILE`.

From a standard PowerShell window at the repository root, the default runner
performs the secret-safe preflight, the two-test disposable-schema module, and
then complete unittest discovery in that order:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\deploy\windows\Run-AuraPostgreSQLTests.ps1
```

Use `-PreflightOnly` or `-Focused` only for bounded diagnostics; neither option
reaches the full suite.

The runner sets `APP_ENV=test`, disables dotenv loading and live provider
credentials only for each child process, validates the fixed loopback target and
least-privilege database identity before discovery, propagates the unittest exit
code, and leaves the caller's environment unchanged. The process-scoped
execution-policy option is needed on hosts that block local scripts; it does not
change the machine or user execution policy.

## Tailscale human gate

Install the current Windows client, sign in interactively through an identity
provider using Personal use, give the device a generic name such as
`aura-demo-node`, and enable MagicDNS, HTTPS certificates, and Funnel through
the browser consent flow. Do not use an auth key or automation token. Do not
paste or record the stable hostname in chat, Git, scripts, or logs.

Funnel is public beta. Anyone can reach an enabled Funnel URL, traffic has
provider bandwidth limits, and this deployment assumes no Funnel SLA. The
gateway's service token, HMAC subject, session, limits, and validation remain
mandatory.

## Manual lifecycle

```powershell
.\Start-AuraPublicDemo.ps1 -Profile staging
.\Test-PublicDemoReadiness.ps1 -Profile staging -AuthenticatedSmoke
.\Stop-AuraPublicDemo.ps1 -Profile staging
```

The start script checks protected configuration, local database readiness,
port ownership, gateway health and route inventory, Funnel status JSON, public
health, and a safe authenticated session create. It never prints the hostname
or credentials. The Funnel process intentionally omits `--bg`; restart the
demo manually after a reboot or Tailscale restart.

Stop disables the profile's Funnel port before stopping AURA. PostgreSQL stays
running. Do not stop the processes by hand unless the lifecycle script reports
a PID ownership error.

## Backup and recovery

`Backup-DemoDatabase.ps1` creates bounded local backups under
`C:\ProgramData\AURA\backups`. `Restore-DemoDatabase-Test.ps1` restores only to
the allowlisted recovery database and requires exact confirmations. Never test
restore against staging or production.

## Staging port fallback gate

Preview normally calls HTTPS 8443 while production calls default HTTPS 443 on
the same stable node hostname. If Vercel cannot reach 8443, stop both profiles
and obtain an explicit decision to run only one profile at a time on public
443. Do not activate both on 443 and do not create an unreviewed hostname.

## Removal

Destructive administrative changes require their exact confirmation strings:

```powershell
.\Unregister-AuraTasks.ps1 -Confirmation UNREGISTER_AURA_TASKS
.\Remove-AuraFirewallRules.ps1 -Confirmation REMOVE_AURA_FIREWALL_RULES
```
