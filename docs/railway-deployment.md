# Railway deployment contract for the AURA demo

This document is configuration inventory, not deployment authorization. It
contains no values for credentials, database URLs, tokens, request IDs, public
references, or user content.

## Service boundary

Use one Railway project with isolated `staging` and `production` environments.
In each environment, create `portfolio-web`, `aura-api`, `postgres`, and
`aura-cleanup`. Only `portfolio-web` receives a public domain. `aura-api` and
`aura-cleanup` have no public domain or TCP proxy. The PostgreSQL TCP proxy must
be disabled after any separately approved migration access is complete.

Set the AURA API service's custom config path to
`/deploy/railway/aura-api.toml`. Set the cleanup service's custom config path to
`/deploy/railway/aura-cleanup.toml`. Both build the reviewed Dockerfile. The API
binds IPv4/IPv6 through `::`, uses Railway's injected `PORT`, runs one worker
and replica, disables access logging, drains for 30 seconds, and checks
`/ready`. No migration is part of build, pre-deploy, or startup.

The cleanup service runs at `17 * * * *` UTC. Its command performs one bounded
100-session pass and exits non-zero with a fixed safe code on failure. Railway
skips a scheduled execution while the preceding execution is still active;
the database operations remain transactional and idempotent.

## Exact AURA API variables

| Name | Classification | Rule |
| --- | --- | --- |
| `APP_ENV` | environment-specific non-secret | Exact value `demo`. |
| `PORT` | environment-specific non-secret | Exact value `8000`; needed for the website reference variable. |
| `DEMO_DATABASE_URL` | Railway reference, sensitive | `${{postgres.DATABASE_URL}}`; seal the resolved value and never add a public TCP URL. The database name must contain `demo`. |
| `SQL_ECHO` | non-secret | Exact value `false`. |
| `DEMO_BFF_SERVICE_TOKEN` | generated secret | Same generated value as website `AURA_DEMO_SERVICE_TOKEN`; service-local and sealed. |
| `AUTH_JWT_SECRET` | generated secret | Independent 32--512 character value; sealed. |
| `AUTH_JWT_ISSUER` | environment-specific non-secret | `aura-demo-staging` or `aura-demo-production`. |
| `AUTH_JWT_AUDIENCE` | environment-specific non-secret | `aura-demo-api-staging` or `aura-demo-api-production`. |
| `AUTH_JWT_EXPIRE_MINUTES` | non-secret | Exact value `60`. |
| `AI_PROVIDER` | non-secret | Exact value `openai` for this topology. |
| `OPENAI_MODEL` | environment-specific non-secret | Start with the reviewed account-supported model; approve the exact name at secret provisioning. |
| `OPENAI_API_KEY` | external provider secret | Environment-specific and sealed. Never share between staging and production. |

Do not set `DATABASE_URL` on AURA in demo mode: setting it to the same Railway
target as `DEMO_DATABASE_URL` is intentionally rejected. Do not provision
`AURA_CLIENT_SUBJECT_HMAC_KEY` on AURA. That key belongs only to the website;
AURA receives and validates only the opaque 64-hex digest.

The cleanup service needs only `APP_ENV=demo` and the same environment's
`DEMO_DATABASE_URL` reference. It does not receive the BFF, JWT, HMAC, or AI
secrets.

## PostgreSQL gate

Select Railway's SSL-enabled PostgreSQL major 16 image and keep the major tag,
not a minor pin. SQLAlchemy 2.0.35 and psycopg 3.2.2 support this target. Each
AURA process uses a bounded five-connection pool, no overflow, a five-second
checkout timeout, pre-ping, LIFO reuse, and a 300-second recycle. With one API
process and one non-overlapping cleanup process, the planned maximum is ten
application connections; confirm the selected Railway tier also has headroom
for migration and administrative connections.

For a new empty database, the exact schema command is `python create_tables.py`
from the reviewed AURA image, run once as an explicitly approved one-off task.
It creates the current model schema directly; do not replay historical
migrations afterward. Verify only table/column/constraint/index metadata and
aggregate row counts, then run the guarded PostgreSQL suite.

There is no deployed Railway baseline yet. If a non-empty database is found,
stop and inventory metadata before selecting a delta. The historical additive
order to evaluate is:

1. `add_customer_id_to_reservations.py`;
2. `add_secure_customer_identity.py`;
3. `add_conversation_workflow_states.py`;
4. `add_support_tickets.py`;
5. `add_support_ticket_notifications.py`;
6. `add_telegram_identities.py`;
7. `add_public_reservation_reference.py`;
8. `allow_public_reference_workflow_schema_v2.py`;
9. `add_demo_persistence.py`;
10. `add_demo_chat_request_id.py`;
11. `add_demo_chat_reservation_mutation.py`;
12. `add_demo_chat_content_safety.py`.

Do not blindly run that list. Classify its metadata-dependent DDL and the public
reference backfill against a restored staging copy first. Table/index creation
is additive but can lock catalogs; constraint validation and reference backfill
can scan or lock affected tables. Take and lock a manual volume backup before
the approved migration. Prefer Railway PITR for production. Rollback is a new
restored database service plus reviewed connection cutover, never destructive
down-migrations.

## Remaining runtime proofs

Before any public traffic, prove private DNS reachability, absence of an AURA
public domain, readiness behavior, provider timeout, cleanup termination,
aggregate-only logs, image non-root execution, image history without secrets,
and the complete staging HTTP/browser matrix. The local Docker daemon was not
available when this configuration was prepared, so both remote builds and
image scans remain mandatory staging gates.

Railway references:

- <https://docs.railway.com/private-networking>
- <https://docs.railway.com/config-as-code>
- <https://docs.railway.com/cron-jobs>
- <https://docs.railway.com/databases/postgresql>
- <https://docs.railway.com/volumes/backups>
- <https://docs.railway.com/volumes/point-in-time-recovery>
