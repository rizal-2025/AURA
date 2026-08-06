# AURA Windows self-host deployment runbook

The selected demo backend is one AURA process and local PostgreSQL on Windows,
reached only through a named Cloudflare Tunnel and protected by Cloudflare
Access Service Auth. Vercel remains the website and BFF. No router port is
forwarded and AURA/PostgreSQL stay on `127.0.0.1`.

The audited entry point is:

```powershell
.\deploy\windows\Start-Aura.ps1 -Profile staging
.\deploy\windows\Start-Aura.ps1 -Profile production
```

It disables repository dotenv loading, requires `APP_ENV=demo`, requires a
local demo PostgreSQL URL, and starts one Uvicorn worker without reload or
access logging. Staging is fixed to `127.0.0.1:8001`; production is fixed to
`127.0.0.1:8000`. Configuration mismatch stops startup.

Cloudflare publishes only `/internal/demo/*` to the matching local port. Local
`/health` and `/ready`, OpenAPI, PostgreSQL, and Windows management surfaces are
not published. A final unmatched route returns HTTP 404. The API hostnames have
Access applications whose only authorization policy is the matching Service
Auth token. AURA still independently requires `X-BFF-Service-Token`.

Cleanup, schema planning/application, backup, and restore are local operations.
The old Koyeb/Neon deployment contract and GitHub Actions cleanup/migration
workflows are removed. No cloud database or GitHub database secret is used.

See `deploy/windows/README.md` for the gated installation, local PostgreSQL,
backup/recovery, Task Scheduler, firewall, tunnel, and rollback procedures.
