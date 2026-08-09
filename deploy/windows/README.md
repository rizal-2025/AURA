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

5. Maintenance registration is a separate activation gate. Do not run this
   command during cleanup hardening or dry-run validation:

   ```powershell
   .\Register-AuraTasks.ps1
   ```

   When separately authorized, this registers hourly cleanup and daily backup.
   It does not register AURA or Funnel startup. The cleanup action uses the
   verified repository root as its explicit Task Scheduler working directory.

## Cleanup hardening contract

The cleanup wrapper is Production-only, defaults to a zero-mutation dry-run,
and validates the repository layout, secret ACLs, exact Production database
target, loopback PostgreSQL listener, and schema readiness before invoking the
Python job.

Preview bounded aggregate counts while Production remains offline:

```powershell
.\Run-DemoCleanup.ps1 -Profile production -Mode DryRun
```

Dry-run uses the same bounded expired-session eligibility query as execute mode
and reports counts only. It does not print session, customer, token, or database
identifiers. Execute mode requires the exact non-secret confirmation token and
must not be used until a separate activation gate authorizes it:

```powershell
.\Run-DemoCleanup.ps1 -Profile production -Mode Execute `
  -Confirmation RUN_AURA_DEMO_CLEANUP
```

Each session is still deleted in its own transaction. A failed session rolls
back completely while independent candidates may continue; any such partial
failure makes the job exit non-zero. Cleanup writes protected aggregate
operation records for dry-run, success, partial failure, and failure.
This hardening does not change the two-hour idle expiry, 24-hour absolute
expiry, revocation eligibility, or the absence of a post-expiry grace period.

`Get-AuraPublicDemoStatus.ps1` reports one of `CLEANUP_NOT_CONFIGURED`,
`CLEANUP_NEVER_RAN`, `CLEANUP_HEALTHY`, `CLEANUP_STALE`, or `CLEANUP_FAILED`.
An absent task is intentionally informational and does not degrade status.
Once installed, an execute success older than three hours is stale, and a later
failed execute is failed. Dry-runs do not count as successful retention runs.

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

The lifecycle scripts discover the signed Tailscale CLI at the official
`Program Files` installation before considering an application found on
`PATH`. Do not modify the system or user `PATH` just for these scripts.

Funnel is public beta. Anyone can reach an enabled Funnel URL, traffic has
provider bandwidth limits, and this deployment assumes no Funnel SLA. The
gateway's service token, HMAC subject, session, limits, and validation remain
mandatory.

## Staging PostgreSQL credential

Provision the additive staging role and database from an elevated local
PowerShell window. PostgreSQL prompts locally for only passwords that are
missing; the values do not enter command history or process arguments:

```powershell
& 'C:\Program Files\PostgreSQL\18\bin\psql.exe' `
  --host=127.0.0.1 --port=5432 --username=postgres --dbname=postgres `
  --set=target=staging --file=.\deploy\windows\Bootstrap-LocalPostgreSQL.sql
```

Then create the separate protected staging pgpass file. Enter the same
`aura_staging_runtime` password at the secure local prompt:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\deploy\windows\Initialize-AuraPostgreSQLStagingCredential.ps1
```

The initializer refuses an existing credential unless `-ReplaceExisting` is
explicit, validates the temporary credential against the fixed staging-only
database and least-privilege role contract, and installs it atomically. It does
not read, replace, or authenticate with `test.pgpass` or a production database.

For a newly bootstrapped empty staging database, initialize its schema from an
elevated local PowerShell window. Enter the existing `aura_migration_owner`
password only at the secure prompt:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\deploy\windows\Initialize-AuraPostgreSQLStagingSchema.ps1
```

The schema initializer uses an ACL-protected temporary pgpass file, requires an
explicit `additive-empty-schema` plan, applies only the model metadata to that
empty staging database, verifies exact convergence, and removes the temporary
credential. It refuses any non-empty non-converged schema and never drops data.

## Production PostgreSQL preparation

Production preparation is a separate human-gated flow. Its fixed role and
database are `aura_public_runtime` and `aura_demo_public`; staging and test
credentials are never accepted as substitutes.

From an elevated local PowerShell at the repository root, run the reviewed
additive bootstrap only after its Production database gate is authorized:

```powershell
& 'C:\Program Files\PostgreSQL\18\bin\psql.exe' `
  --host=127.0.0.1 --port=5432 --username=postgres --dbname=postgres `
  --set=target=production --file=.\deploy\windows\Bootstrap-LocalPostgreSQL.sql
```

The bootstrap prompts locally only for missing role passwords, never drops a
database or schema, and does not rotate an existing password automatically.

After the reviewed additive bootstrap has created the Production role and
database, create the protected runtime credential. Enter the existing
`aura_public_runtime` password only at the secure local prompt:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\deploy\windows\Initialize-AuraPostgreSQLProductionCredential.ps1
```

The initializer is fixed to `production.pgpass`, authenticates through a
password-free URL, verifies least privilege and denial of test/staging database
access, and atomically installs the credential only after the preflight passes.

For a newly bootstrapped empty Production database, run the separate empty-only
schema initializer and enter the existing `aura_migration_owner` password only
at its secure prompt:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\deploy\windows\Initialize-AuraPostgreSQLProductionSchema.ps1
```

The wrapper requires a zero-table plan, exact ten-table convergence after
apply, and an independent verify. It never drops or repairs a partial schema
and removes its temporary migration credential unconditionally.

## One-command Production lifecycle

```powershell
.\Start-AuraPublicDemo.ps1 -Profile production
.\Get-AuraPublicDemoStatus.ps1 -Profile production
.\Stop-AuraPublicDemo.ps1 -Profile production
.\Invoke-AuraProductionBackup.ps1 -Profile production
```

The start script checks protected configuration and pgpass ACLs, the fixed
Production database role/target and exact ten-table schema, local process and
listener ownership, firewall rules, foreground Funnel ownership, and public
health. Ordinary start and readiness never create a session or mutate the
database. The Funnel process intentionally omits `--bg`; restart the demo
manually after a reboot or Tailscale restart.

Stop terminates only the exact owned foreground Funnel before stopping the exact
owned AURA process. It does not use routine Funnel or Serve resets. PostgreSQL
stays running. Ambiguous process ownership fails closed at a human gate.

See `docs/windows-production-operations.md` for the operator runbook and
`docs/PUBLIC-DEMO-MANUAL.md` for the short Indonesian manual.

## Backup and recovery

`Backup-DemoDatabase.ps1` creates bounded local backups under
`C:\ProgramData\AURA\backups` with inheritance disabled and access limited to
the current operator, Administrators, and SYSTEM. `Protect-AuraBackup.ps1`
repairs and validates an exact pre-existing backup only after its explicit
`PROTECT_AURA_BACKUP` confirmation. `Restore-DemoDatabase-Test.ps1` rejects
backups without that protected ACL, accepts an exact source profile and matching
backup filename, securely prompts for the migration owner password, and restores
only to the allowlisted `aura_restore_test` database. It requires exact ten-table
schema verification and explicit confirmations. Never test restore against
staging or production. Dropping the restore-test database is optional and
requires the separate exact `DROP_AURA_RESTORE_TEST` confirmation.

If an interrupted or generically reported restore already created
`aura_restore_test`, do not run restore again. `Test-AuraRestoredDatabase.ps1`
uses a transaction-read-only session, metadata-only exact schema inspection,
and an aggregate table estimate to validate that existing database. It cannot
create, restore, or drop a database and requires the exact
`VERIFY_EXISTING_AURA_RESTORE_TEST` confirmation. The schema job permits this
fixed password-free loopback target only for `verify`; the global demo database
policy and all plan/apply operations remain unchanged.

After successful restore verification, cleanup remains destructive and separate.
`Remove-AuraRestoreTestDatabase.ps1` requires the exact
`DROP_AURA_RESTORE_TEST` confirmation, rechecks the fixed database owner and
exact schema under read-only enforcement, calls `dropdb` without force, and
independently verifies absence afterward. It cannot target any other database.

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
