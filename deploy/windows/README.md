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
