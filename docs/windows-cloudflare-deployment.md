# Windows, local PostgreSQL, and Cloudflare contract

## Boundary

```text
Vercel BFF -> Cloudflare Access -> named Tunnel -> 127.0.0.1:8000/8001
                                                    |
                                                    +-> 127.0.0.1:5432
```

- Production and staging use separate hostnames, Access applications, service
  tokens, AURA service tokens, JWT secrets, AI credentials, and databases.
- The tunnel is remotely managed. Its service token is stored only by the
  Windows `cloudflared` service installation, never in Git or AURA files.
- Only the internal demo path is routed. An unmatched path returns 404.
- API caching is disabled at Cloudflare. The BFF also uses `no-store`.
- Direct unauthenticated requests are denied by Access; AURA authentication
  remains mandatory if Access is bypassed or misconfigured.

## Local invariants

- AURA: exactly `127.0.0.1:8000` (production) or `127.0.0.1:8001` (staging),
  one worker, no reload, no debug, no access log.
- PostgreSQL: `listen_addresses = '127.0.0.1'`, port 5432, loopback-only
  `pg_hba.conf` rules, password authentication using SCRAM.
- Windows Firewall: explicit inbound blocks for 8000, 8001, and 5432; no allow
  rule or router forwarding for those ports.
- Logs and backups are under `C:\ProgramData\AURA` with bounded retention.
- Secret files are under `C:\ProgramData\AURA\secrets`, inheritance disabled,
  and readable only by the protected task account, Administrators, and SYSTEM.

## Availability

This is a personal demo, not a highly available service. The public flow is
unavailable while the laptop, home internet, PostgreSQL, AURA, or `cloudflared`
is down. The Vercel BFF converts that state to a fixed 503 response without
revealing the origin, Cloudflare response, database, or host details.

Current official references:

- <https://developers.cloudflare.com/tunnel/routing/>
- <https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/>
- <https://developers.cloudflare.com/cloudflare-one/access-controls/policies/>
