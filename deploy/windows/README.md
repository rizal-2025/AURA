# AURA Windows deployment assets

These files prepare the self-hosted demo. They do not authorize account work,
secret provisioning, database creation/migration, elevated installation,
deployment, live provider calls, or public go-live.

## Profiles and directories

| Profile | AURA | Database | Secret file |
| --- | --- | --- | --- |
| staging | `127.0.0.1:8001` | `aura_demo_staging` | `C:\ProgramData\AURA\secrets\staging.conf` |
| production | `127.0.0.1:8000` | `aura_demo_public` | `C:\ProgramData\AURA\secrets\production.conf` |

The test database is `aura_test`. Logs are in `C:\ProgramData\AURA\logs` and
backups in `C:\ProgramData\AURA\backups`. The scripts accept only the two fixed
profiles and fixed allowlisted roots.

## Secret provisioning gate

Copy `secrets.template.conf` manually for each profile. Do not paste values into
chat. Use distinct staging/production values. Every configured secret is at
least 32 random bytes; issuer/audience are non-default environment labels;
expiry is `60`; provider timeout is 1–30 seconds; `SQL_ECHO=false`; retention is
1–365 days. Set the unused provider fields to a non-secret sentinel label only
when the selected provider does not read them.

Required runtime names are `APP_ENV`, `DEMO_DATABASE_URL`,
`DEMO_BFF_SERVICE_TOKEN`, `AUTH_JWT_SECRET`, `AUTH_JWT_ISSUER`,
`AUTH_JWT_AUDIENCE`, `AUTH_JWT_EXPIRE_MINUTES`, `AI_PROVIDER`, the matching
provider model/key or local URL/model, `AI_PROVIDER_TIMEOUT_SECONDS`, and
`SQL_ECHO`. Delete the unused provider-specific lines instead of filling them.
Backup tooling also requires `AURA_DB_HOST`, `AURA_DB_PORT`,
`AURA_DB_NAME`, `AURA_DB_USER`, `AURA_MIGRATION_USER`, `PGPASSFILE`, and the two
retention settings. `AURA_DB_HOST` is exactly `127.0.0.1`, the port is `5432`,
and `PGPASSFILE` is an ACL-restricted file below the secret directory.

Disable inheritance and grant the narrow ACL in an elevated terminal after the
files are filled locally:

```powershell
icacls 'C:\ProgramData\AURA\secrets' /inheritance:r
icacls 'C:\ProgramData\AURA\secrets' /grant:r 'SYSTEM:(OI)(CI)F' 'Administrators:(OI)(CI)F' "$env:USERDOMAIN\$env:USERNAME:(OI)(CI)R"
icacls 'C:\ProgramData\AURA\secrets\staging.conf' /inheritance:r /grant:r 'SYSTEM:F' 'Administrators:F' "$env:USERDOMAIN\$env:USERNAME:R"
icacls 'C:\ProgramData\AURA\secrets\production.conf' /inheritance:r /grant:r 'SYSTEM:F' 'Administrators:F' "$env:USERDOMAIN\$env:USERNAME:R"
icacls 'C:\ProgramData\AURA\secrets\pgpass.conf' /inheritance:r /grant:r 'SYSTEM:F' 'Administrators:F' "$env:USERDOMAIN\$env:USERNAME:R"
```

## PostgreSQL migration gate

`Bootstrap-LocalPostgreSQL.sql` is additive: it creates four non-superuser
roles and one approved target database per invocation, revokes public
database/schema creation, and sets current/default privileges. It never drops a
database, schema, table, or row. Passwords are prompted without echo and are
not command arguments. Exact targets are `test`, `staging`, and `production`;
approve and invoke each separately.

Before running it, back up any existing databases with the same names and
verify PostgreSQL has `listen_addresses = '127.0.0.1'`, password encryption is
SCRAM, and `pg_hba.conf` permits only loopback. Run the template using a local
PostgreSQL administrator only after approval:

```powershell
psql.exe --host=127.0.0.1 --port=5432 --username=postgres --dbname=postgres -v target=test --file='.\deploy\windows\Bootstrap-LocalPostgreSQL.sql'
```

For `aura_test`, set a process-only `TEST_DATABASE_URL` that targets exactly
that database, run the full suite from a clean working directory so no
repository dotenv file is read, and remove disposable schemas after the test.
Never point tests at either demo database.

For staging, separately approve and run the same command with
`-v target=staging`, set the staging configuration, then run `demo_schema plan`,
`apply-empty-schema`, and `verify`. Re-run the staging target to grant runtime
access to created tables, then take a staging backup. Repeat with
`-v target=production` only under a separate production approval for
`aura_demo_public`. Rollback restores the
pre-migration backup to a separate recovery database; additive schema changes
are not automatically reversed.

## Cloudflare account/domain gate

Use a named, remotely managed tunnel. In Cloudflare Zero Trust, publish:

- `aura-staging.<YOUR_CLOUDFLARE_DOMAIN>` with path
  `^/internal/demo/.*$` to `http://127.0.0.1:8001`;
- `aura-api.<YOUR_CLOUDFLARE_DOMAIN>` with the same path to
  `http://127.0.0.1:8000`;
- a final unmatched HTTP status 404 route.

Do not publish `/health`, `/ready`, PostgreSQL, OpenAPI, RDP, file shares, or a
bastion. Disable caching for both API hostnames. Create one self-hosted Access
application per hostname. Each has only a `Service Auth` policy whose Include
selector is the matching environment's service token. Do not add Allow,
Everyone, Bypass, email login, or token sharing between environments.

The Vercel BFF sends `CF-Access-Client-Id` and
`CF-Access-Client-Secret` on every request. Cloudflare documents that
service-token-only applications require the service credential on each
request. Rotate by creating a replacement token, updating the matching Vercel
environment, validating it, and revoking the old token.

## Elevated Windows installation gate

After the tunnel dashboard supplies its exact Windows command, paste that
command only into your own elevated terminal. The token must never be placed in
this repository, a report, shell transcript, or chat. For a remotely managed
tunnel the dashboard command has this form:

```powershell
& 'C:\Cloudflared\bin\cloudflared.exe' service install <PASTE_TOKEN_ONLY_HERE>
```

Then, from an elevated PowerShell in the repository:

```powershell
.\deploy\windows\Install-AuraFirewallRules.ps1 -Confirmation INSTALL_AURA_FIREWALL_RULES
.\deploy\windows\Register-AuraTasks.ps1
powercfg.exe /change standby-timeout-ac 0
.\deploy\windows\Test-LocalHostSecurity.ps1
Get-Service cloudflared, postgresql* | Select-Object Name, Status, StartType
Get-ScheduledTask -TaskName 'AURA*' | Select-Object TaskName, State
```

Task registration also removes inherited access from the log, backup, and run
directories and grants access only to SYSTEM, Administrators, and the protected
task account. The power change affects sleep while plugged in only. Keep display timeout,
battery sleep, lock screen, antivirus, firewall, and update protections intact.
Configure PostgreSQL and `cloudflared` services for automatic startup. Network
reconnect is handled by `cloudflared`; the API task starts at protected-user
logon and ignores duplicate starts. Cleanup runs hourly at minute 17 local
(also minute 17 UTC) without overlap. Backup runs daily at 02:41 local.

## Backup, restore, and rollback

`Backup-DemoDatabase.ps1` uses `pg_dump` custom format, a password file rather
than a password argument, timestamped no-overwrite files, non-empty validation,
and bounded retention. The newest successful backup is preserved.

`Restore-DemoDatabase-Test.ps1` accepts only a `.dump` under the allowlisted
backup root and restores only into absent `aura_restore_test`. It requires the
literal restore confirmation. Dropping that temporary database requires a
second literal confirmation. Output contains only timestamp/size, names,
schema counts, and aggregate row estimates.

Application rollback stops the current profile and starts the previous audited
commit with its matching sealed configuration. Database rollback restores into
a separate recovery database first; never overwrite a live database. Removing
tasks or firewall rules requires the exact confirmation values defined by the
scripts.

## Availability

The demo is available only while the laptop is powered, awake, online, and all
four local components (PostgreSQL, AURA, `cloudflared`, scheduled tasks) are
healthy. Power loss, sleep, Windows restart, or home-internet failure produces a
temporary fixed 503 at the Vercel BFF; there is no 24/7 availability guarantee.
