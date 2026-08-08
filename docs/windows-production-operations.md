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

Production cleanup is never part of start or status. Use the existing dedicated
cleanup command only under its documented policy and review aggregate output;
do not schedule new destructive cleanup as part of this lifecycle.

## What not to do

- No router port forwarding.
- No direct port 8000 exposure or LAN bind.
- No manual pgpass editing.
- No random Python or Tailscale process killing.
- No Funnel reset or Serve reset as routine recovery.
- No Vercel secret copy to chat, tickets, or logs.
- No Production database manual mutation.
- No automatic public Funnel task at logon or boot.
