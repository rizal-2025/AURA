# AURA demo deployment runbook

This runbook is provider-independent. It prepares a deployment but does not
authorize migration, secret provisioning, staging, or public traffic.

## Runtime contract

- Build the repository `Dockerfile` from a reviewed commit.
- Run exactly one Uvicorn worker and one application replica. AURA's current
  conversation lock is process-local; horizontal API scaling is not supported.
- Terminate TLS at Koyeb's public ingress. Only the authenticated website BFF may
  call `/internal/demo/*`; direct browser attempts fail closed.
- Use `/health` for liveness and `/ready` for database readiness. The readiness
  response is fixed and never exposes connection or SQL detail.
- Do not run migrations from application startup or the container command.
- Keep access logging disabled until the provider has a reviewed redaction
  policy. Application logs already use fixed operational codes.

The image runs as an unprivileged user, contains only application and migration
code, and starts Uvicorn with `--workers 1 --no-access-log`.

## Required variable names

Provision values only in the selected platform's secret manager. Never put
values in Git, image layers, build arguments, logs, or support chat.

- `APP_ENV`
- `DATABASE_URL`
- `DEMO_DATABASE_URL`
- `SQL_ECHO`
- `DEMO_BFF_SERVICE_TOKEN`
- `AUTH_JWT_SECRET`
- `AUTH_JWT_ISSUER`
- `AUTH_JWT_AUDIENCE`
- `AUTH_JWT_EXPIRE_MINUTES`
- `AI_PROVIDER`
- the variables required by the selected AI provider

For the isolated public demo, `APP_ENV` must be `demo`, SQL echo must be off,
and the demo database must be a dedicated PostgreSQL target accepted by AURA's
fail-closed configuration validation.

## Cleanup scheduler

Schedule one bounded process at a time:

```text
python -m app.jobs.demo_cleanup --once --batch-size 100
```

GitHub Actions supplies the single-run scheduler with a direct Neon maintenance
URL, shared per-environment maintenance concurrency, bounded timeout, and
non-zero failure status. The hourly schedule remains inert until a post-go-live
repository variable enables it.

## Migration gate

For a new empty Neon database, use the controlled metadata-only runner:

```text
python -m app.jobs.demo_schema --operation apply-empty-schema
```

It blocks non-empty partial schemas, applies current metadata only to an empty
database, and verifies aggregate schema metadata. For an existing baseline, the
repository uses explicit idempotent Python migration scripts rather than a
version ledger. Identify the deployed baseline and produce an exact ordered
delta. Demo-related candidates include:

- `add_secure_customer_identity.py`;
- `add_demo_persistence.py`;
- `add_demo_chat_request_id.py`;
- `add_public_reservation_reference.py`;
- `allow_public_reference_workflow_schema_v2.py`;
- `add_demo_chat_reservation_mutation.py`;
- `add_demo_chat_content_safety.py`.

Do not infer that every candidate is required. At the production-migration
gate, capture a database backup, classify every statement, estimate lock impact,
dry-run against a restored staging copy, and verify only schema metadata and
aggregate counts. Never print rows, credentials, tokens, or public references.

## Rollback

Application rollback means routing traffic to the previously verified image.
The listed schema changes are intended to be additive; do not automatically
reverse them or delete data. If a migration fails, stop traffic, retain the
backup and logs with safe codes only, and perform a reviewed recovery. Public
traffic remains closed until readiness, cleanup, migration, provider timeout,
rate-limit, and client-subject protections all pass staging verification.

## Outstanding runtime gates

Provider accounts, secrets, databases, migrations, deployment, remote image
capacity, cold start, browser QA, monitoring, and public traffic remain gated.
The selected architecture is Vercel Hobby, Koyeb Free, Neon Free, and GitHub
Actions; see `koyeb-neon-github-deployment.md` for the exact value-free contract.
