# Koyeb, Neon, and GitHub Actions deployment contract for AURA demo

This value-free runbook prepares the provider migration. It does not authorize
account creation, secret entry, database creation, migration, workflow execution,
deployment, domain changes, or public traffic.

## Runtime topology and capacity

Create one Koyeb App with one Web Service named `aura-api`, sourced from
`rizal-2025/AURA` and the reviewed `master` commit. Use the repository
Dockerfile, the Free Instance, and Frankfurt. Disable deploy-on-push initially;
promote only exact audited commits.

Koyeb Free supplies one organization-wide instance with 512 MB RAM, 0.1 vCPU,
and 2 GB ephemeral disk. It cannot run a Worker Service or persistent volume
and sleeps after one idle hour. The service therefore runs one Uvicorn worker,
one replica, no reload, no access log, and no local persistence. A remote
cold-start and memory proof remains mandatory before public readiness.

Expose HTTP port `8000`, route `/`, and configure HTTP `GET /health` with a
30-second grace period, 60-second interval, 5-second timeout, and three restart
attempts. `/health` is process liveness; check `/ready` separately after deploy
to verify Neon without making database sleep a container-start requirement.

The image runs as unprivileged user `aura`, uses Koyeb's injected `PORT`, and
does not run migrations or cleanup at startup. The Docker build context excludes
Git, environment files, tests, caches, documentation, workflows, and backup
folders. Koyeb does not require a repository-specific manifest for this setup;
record the dashboard settings in the sanitized deployment report.

## Public ingress security

Koyeb assigns a public TLS endpoint. Every `/internal/demo/*` business route
requires an exact `X-BFF-Service-Token`; every client-limited route also requires
lowercase 64-hex `X-Demo-Client-Subject`. Service-token comparison is performed
on fixed-length digests with constant-time comparison. Tokens are never accepted
from query, body, or cookie.

Internal demo routes are excluded from OpenAPI. Existing non-demo product routes
retain their established contracts; they do not bypass service authentication on
`/internal/demo/*`. Health is public and fixed, while readiness is hidden from
schema. Request bodies and provider deadlines stay bounded, responses are safe
envelopes, and access logging remains off.

TLS, a high-entropy service token, exact request DTOs, per-session/global/client
rate limits, and no browser access form a sufficient boundary for this personal
low-traffic demo. A signed-request protocol was not added: without shared replay
state it would add bespoke crypto and race risk while not replacing TLS or token
rotation. Revisit only if staging threat evidence requires it. A pre-auth
application limiter was also not added because an unauthenticated global bucket
would let an attacker deny service to all legitimate users; provider edge abuse
controls and cheap constant-time denial are safer at this scale.

## Exact Koyeb variables

During staging, attach only staging values. During promotion, replace every
environment-specific value with its distinct production counterpart and redeploy
the same audited commit.

| Name | Class | Rule |
| --- | --- | --- |
| `APP_ENV` | non-secret | Exact `demo`. |
| `PORT` | non-secret | Exact `8000`. |
| `DEMO_DATABASE_URL` | Neon pooled secret URL | Isolated environment database; database name contains `demo`; SSL required. |
| `SQL_ECHO` | non-secret | Exact `false`. |
| `DEMO_BFF_SERVICE_TOKEN` | generated secret | Same environment's Vercel `AURA_DEMO_SERVICE_TOKEN`; 32–512 characters. |
| `AUTH_JWT_SECRET` | generated secret | Independent 32–512 character value. |
| `AUTH_JWT_ISSUER` | non-secret | Distinct staging/production deployed label. |
| `AUTH_JWT_AUDIENCE` | non-secret | Distinct staging/production deployed label. |
| `AUTH_JWT_EXPIRE_MINUTES` | non-secret | Exact `60`. |
| `AI_PROVIDER` | non-secret | Exact approved provider, initially `openai`. |
| `OPENAI_MODEL` | non-secret | Account-supported reviewed model name. |
| `OPENAI_API_KEY` | external secret | Unique per environment and never used by tests. |

Do not set `DATABASE_URL` to the same target as `DEMO_DATABASE_URL`. Do not give
Koyeb the website client-subject HMAC key. Session retention, rate limits, and
the 30-second provider deadline remain reviewed source constants rather than
unvalidated environment overrides.

## Neon isolation and connection contract

Prefer two Free projects in AWS Europe (Frankfurt, `aws-eu-central-1`): one
staging and one production. If account constraints later prevent two projects,
stop and approve isolated branches and credentials explicitly. Never share one
schema between environments.

- Koyeb `DEMO_DATABASE_URL`: Neon pooled runtime URL (`-pooler` endpoint), with
  a two-connection SQLAlchemy pool, zero overflow, five-second checkout timeout,
  pre-ping, LIFO reuse, and 300-second recycling.
- GitHub environment secret `NEON_MAINTENANCE_DATABASE_URL`: direct Neon URL for
  cleanup and controlled migration; the single-run schema tool uses one
  connection and never prints the URL.
- `TEST_DATABASE_URL`: disposable guarded staging-test branch only. Never point
  it at staging application data or production.

The empty-database migration path is
`python -m app.jobs.demo_schema --operation apply-empty-schema`. It first blocks
any partial or unknown public schema, creates only current model metadata when
empty, then verifies exact table, column, and primary-key aggregates. It reads no
row data and emits safe aggregate JSON. Existing databases require a separate
reviewed delta and restore plan; no historical migration list is replayed
automatically.

## GitHub Actions maintenance

`demo-cleanup.yml` supports manual staging/production runs and the UTC schedule
`17 * * * *`. The scheduled job is inert until repository variable
`AURA_DEMO_CLEANUP_SCHEDULE_ENABLED` is exact `true`, which must happen only
after public-go-live approval. Scheduled execution targets the protected
`production` environment.

`demo-migration.yml` is `workflow_dispatch` only. Inputs choose `staging` or
`production`, and `plan` or `apply-empty-schema`. Apply requires the exact
40-hex current default-branch SHA. Both workflows:

- have only `contents: read` permission;
- use pinned checkout/setup action revisions and exact Python 3.12.7;
- install the fully pinned requirements set without dependency resolution;
- share a per-environment maintenance concurrency group with no cancellation;
- run only the latest default-branch SHA with bounded timeouts;
- use environment-scoped direct URL secrets;
- never execute on pull requests or forks;
- log only fixed status codes and aggregate counts.

Configure GitHub environments `staging` and `production`. The repositories are
public, so GitHub Free supports environment secrets and required reviewers.
Protect production, prevent self-review where another reviewer is available,
and disallow administrator bypass where supported.

Do not run the first staging or production migration before the separately
documented `HUMAN_GATE_PRODUCTION_MIGRATION` approval.

## Sequential staging and promotion

1. Create isolated Neon staging and production projects at the account gate.
2. Configure GitHub environments and provider variables at the secret gate.
3. Approve and run the staging migration; run all guarded PostgreSQL tests on a
   disposable staging-test branch.
4. Deploy the one Koyeb service with staging values, then validate health,
   readiness, cold start, memory, logs, authentication, and Vercel Preview.
5. After all staging gates and go-live approval, replace Koyeb variables with
   production values and redeploy the exact audited commit. Staging AURA is then
   unavailable during the promotion window.

Rollback restores the previous Koyeb deployment plus its matching sealed
configuration. Database rollback is a reviewed Neon branch/project connection
cutover, never an automatic destructive down-migration.

Official references:

- <https://www.koyeb.com/docs/reference/instances>
- <https://www.koyeb.com/docs/run-and-scale/scale-to-zero>
- <https://www.koyeb.com/docs/build-and-deploy/deploy-with-git>
- <https://www.koyeb.com/docs/reference/secrets>
- <https://www.koyeb.com/docs/run-and-scale/health-checks>
- <https://neon.com/pricing>
- <https://neon.com/docs/connect/connection-pooling>
- <https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax>
- <https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments>
