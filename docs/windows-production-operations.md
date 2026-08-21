# AURA production operations on Windows

This runbook operates the public demo manually. Production remains off after a
Windows restart until an operator runs the start command. Run commands from the
AURA repository root in a normal PowerShell window. The scripts never display
the public address or secret values.

## Start demo

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\deploy\windows\Start-AuraPublicDemo.ps1 -Profile production
```

Success ends with `AURA_PUBLIC_DEMO_READY profile=production`. A second start is
safe and ends with `AURA_PUBLIC_DEMO_ALREADY_READY profile=production`.

## Check status

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\deploy\windows\Get-AuraPublicDemoStatus.ps1 -Profile production
```

The final state is `ready`, `offline`, or `degraded`. Check the fixed
`reason_codes` line when the state is degraded. Status performs no session or
database mutation.

Backup age policy is: `fresh` through 24 hours, `warning` after 24 through 48
hours, `stale` after 48 hours, and `missing` when no Production archive exists.

## Stop demo

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\deploy\windows\Stop-AuraPublicDemo.ps1 -Profile production
```

Success ends with `AURA_PUBLIC_DEMO_STOPPED profile=production`. Repeating the
command is safe. It stops the owned foreground Funnel first and the owned AURA
gateway second. It does not stop PostgreSQL or the Tailscale service.

## Create backup

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  .\deploy\windows\Invoke-AuraProductionBackup.ps1 -Profile production
```

The command verifies the fixed Production database and schema, creates a new
archive without overwriting an existing archive, protects its ACL, validates
the archive, and applies configured retention. Output contains only timestamp
class, byte count, and validation flags. Restore remains a separate human-gated
test-database procedure.

## If demo says SERVICE_UNAVAILABLE

1. Run the status command.
2. If state is `offline`, run Start demo once.
3. If `POSTGRESQL_NOT_RUNNING` appears, start the installed PostgreSQL Windows
   service normally, then run status again. Do not restart it if it is running.
4. If an ACL, target, listener, firewall, or ownership reason appears, stop and
   ask the deployment owner to correct that exact gate. For
   `FIREWALL_INVALID`, the deployment owner must run this reviewed one-time
   command from an elevated PowerShell window:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
     .\deploy\windows\Install-AuraFirewallRules.ps1 `
     -Confirmation INSTALL_AURA_FIREWALL_RULES
   ```

   Run status again afterward; do not bypass the firewall check.
5. If status is `ready` but the website still reports unavailable, verify the
   Vercel Production deployment independently. Do not copy secrets into chat.

## After Windows restart

1. Confirm the PostgreSQL and Tailscale Windows services are running.
2. Run the status command; `offline` is the expected safe state.
3. Run Start demo.
4. Run status again and share the already-approved link only when state is
   `ready`.

No task starts AURA or Funnel automatically. Do not add one.

## If Funnel is stale

- Run status first.
- `FUNNEL_PID_STALE` means metadata points to an absent process; the next start
  may remove only that stale metadata and safely recreate Funnel.
- `FUNNEL_OWNERSHIP_AMBIGUOUS` is a human gate. Do not reset Funnel, reset Serve,
  or kill a Tailscale process. Have the deployment owner inspect the exact
  executable, command, profile, and creation time.
- Never enable background Funnel persistence for this deployment.

## If AURA is stale

- `AURA_PID_STALE` is safe stale metadata; start may remove only that metadata.
- `AURA_PROCESS_OWNERSHIP_UNCERTAIN` or `AURA_PROCESS_OWNERSHIP_AMBIGUOUS` means
  the lifecycle cannot prove that a process belongs to this deployment. Do not
  kill Python processes. Ask the deployment owner to resolve ownership.
- An unexpected port 8000 listener is always a human gate.

## Emergency stop

Run the normal Stop demo command first. It enforces this order:

1. validate the exact owned Production Funnel process;
2. stop Funnel and verify public health is unavailable;
3. validate the exact owned AURA process;
4. request graceful AURA termination, wait a bounded time, and force only the
   same verified process if necessary;
5. verify port 8000 is closed.

If ownership is ambiguous, do not improvise process termination. Disconnect
the machine from the network through the approved Windows control, preserve
evidence, and contact the deployment owner.

## Manual cleanup

Production cleanup is never part of start. Status reports cleanup configuration
and health but never invokes cleanup or queries session data. Before activation,
no activation marker plus no task (or a correctly staged disabled task) is
`CLEANUP_NOT_CONFIGURED` and is informational.

Use `Run-DemoCleanup.ps1 -Profile production -Mode DryRun` for the approved
zero-mutation preview. It reports bounded aggregate eligible-row counts without
identifiers. Execute mode requires `-Mode Execute -Confirmation
RUN_AURA_DEMO_CLEANUP`, preserves CLI exits 0/1/2 exactly, and
must not be used until a separate activation gate authorizes it.

Protected operation logs record timestamp, profile, mode, aggregate session
eligibility/attempt/success/failure counts, final result, and elapsed time.
An authorized `Register-AuraTasks.ps1` run only stages cleanup disabled. Its
fixed XML definition uses an hourly `PT1H` repetition from local minute 17,
SYSTEM/ServiceAccount/LeastPrivilege, and `IgnoreNew` overlap handling.
A registered/exported Windows definition may omit the explicit defaults
`RunLevel=LeastPrivilege` and `StartWhenAvailable=false`, and Windows may omit
`Enabled=true` after a disabled task is enabled. Canonical validation accepts
those omissions only when a fresh read of the exact registered task independently
reports Limited, `StartWhenAvailable=false`, and the expected enabled state.
Disabled staging still requires explicit `Enabled=false`; arbitrary missing,
malformed, contradictory, or changed values remain invalid. A separate elevated
`Activate-AuraDemoCleanup.ps1` confirmation validates the
task and prerequisites, writes a non-secret version-2 `activating` marker under
`C:\ProgramData\AURA\run`, enables and revalidates the task, then atomically
transitions the marker to `active`. Execute mode refuses to invoke Python unless
the marker is `active` and the exact task is enabled, so a scheduled launch
during transition cannot mutate cleanup data. Enable, validation, or transition
failure disables the task before removing the marker. `Deactivate-AuraDemoCleanup.ps1`
disables and validates first, then removes either valid marker state.
Repository Python readiness operations launched by the elevated activation path
derive and explicitly use the repository root as their working directory; they
do not depend on the caller's current directory.

After activation, status classifies task drift as `CLEANUP_TASK_MISSING`,
`CLEANUP_TASK_DISABLED`, or `CLEANUP_TASK_INVALID`; no successful execute as
`CLEANUP_NEVER_RAN`; a success older than three hours as `CLEANUP_STALE`; and
the latest failed/partial execute as `CLEANUP_FAILED`. Each is not-ready.
The last execute attempt, last dry-run, and last successful execute are tracked
separately; dry-run success never satisfies execute freshness.
Unexpected task removal never clears the marker.
An incomplete `activating` state is reported as
`CLEANUP_ACTIVATION_INCOMPLETE` and is never readiness-compatible.

Do not register, enable, or execute scheduled cleanup as part of the normal
start/stop lifecycle.

## What not to do

- No router port forwarding.
- No direct port 8000 exposure or LAN bind.
- No manual pgpass editing.
- No random Python or Tailscale process killing.
- No Funnel reset or Serve reset as routine recovery.
- No Vercel secret copy to chat, tickets, or logs.
- No Production database manual mutation.
- No automatic public Funnel task at logon or boot.
