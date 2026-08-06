# Windows and Tailscale Funnel deployment contract

The public demo path is:

```text
Vercel BFF -> public HTTPS Funnel -> 127.0.0.1:8000 or 8001 -> local PostgreSQL
```

Funnel is a public internet boundary. It does not authenticate visitors with
tailnet identity, ACLs, source IP, or CORS. AURA therefore retains the
constant-time `X-BFF-Service-Token` check, BFF-derived HMAC client subject,
HttpOnly browser session cookie, request framing/body bounds, strict DTOs,
database-backed rate limits, and a 16-request gateway concurrency cap.

The dedicated `app.funnel_main` ASGI application exposes exactly `/health` and
the five `/internal/demo/*` operations used by the BFF. Root, readiness,
OpenAPI/docs, public chat/reservation, Telegram, and administrative routes are
absent and return 404 without slash redirects.

Profiles are fixed:

| Profile | Public Funnel port | Loopback target |
|---|---:|---|
| staging | 8443 | `http://127.0.0.1:8001` |
| production | 443 | `http://127.0.0.1:8000` |

Tailscale documents 443, 8443, and 10000 as the only Funnel ports. This
deployment uses no `--set-path` and no `--bg`. The CLI remains a foreground
session supervised by a PID file, so a reboot or Tailscale restart does not
silently republish the demo. `tailscale funnel status --json` is parsed in
memory; the node hostname is neither printed nor written to logs.

Manual lifecycle:

```powershell
.\deploy\windows\Start-AuraPublicDemo.ps1 -Profile staging
.\deploy\windows\Test-PublicDemoReadiness.ps1 -Profile staging -AuthenticatedSmoke
.\deploy\windows\Stop-AuraPublicDemo.ps1 -Profile staging
```

Startup validates the profile and protected secret file, checks local
PostgreSQL, refuses an occupied loopback port, starts the minimal gateway,
checks route inventory, starts Funnel, parses status JSON, verifies public
health, and creates one bounded authenticated smoke-test session. Failure rolls
back Funnel before AURA. Stop always disables Funnel before AURA and never
stops PostgreSQL.

Task Scheduler registers cleanup and backup only. AURA and Funnel have no
boot/logon task and remain off by default. Firewall rules block inbound TCP
8000, 8001, and 5432; no inbound allow rule is created.

Funnel is currently beta, has non-configurable bandwidth limits, and should not
be treated as an SLA-backed origin. If Vercel cannot call port 8443, stop both
profiles and make an explicit one-profile-at-a-time decision before assigning
staging to public port 443. Never improvise a second hostname or weaken the BFF
boundary.

Official references:

- <https://tailscale.com/docs/features/tailscale-funnel>
- <https://tailscale.com/docs/reference/tailscale-cli/funnel>
- <https://tailscale.com/docs/features/magicdns>
