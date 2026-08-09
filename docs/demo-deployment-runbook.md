# AURA public demo runbook

The Windows host runs the dedicated Funnel gateway on loopback and local
PostgreSQL on loopback. Tailscale Funnel is the only public transport, and the
Vercel BFF is the only intended authenticated caller. Funnel itself is public;
all authorization remains in AURA and the BFF.

Use `deploy/windows/README.md` for installation, ACL, database, firewall,
backup, recovery, manual start/stop, and validation procedures. Use
`docs/windows-tailscale-funnel-deployment.md` for the boundary contract and
port decision gate.

Normal operation is manual and off by default:

```powershell
.\deploy\windows\Start-AuraPublicDemo.ps1 -Profile production
.\deploy\windows\Test-PublicDemoReadiness.ps1 -Profile production -AuthenticatedSmoke
.\deploy\windows\Stop-AuraPublicDemo.ps1 -Profile production
```

Only cleanup and backup may be scheduled. Never register AURA or Funnel at
boot/logon. Never expose PostgreSQL, RDP, file shares, OpenAPI, `/ready`, or the
full `app.main` application. Never log the stable `*.ts.net` hostname, service
token, session token, database URL, provider key, or Tailscale credential.

Cleanup scheduling requires its own human activation gate. Before that gate,
keep `AURA Demo Cleanup` absent and use only the Production-offline dry-run
documented in `deploy/windows/README.md`. An absent task is reported as
`CLEANUP_NOT_CONFIGURED` without degrading normal status.
