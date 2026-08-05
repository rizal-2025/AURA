# Demo Safe-Content Boundary - Phase E

## Decision

The AURA demo backend is approved for BFF consumption after the Phase E
controls in this branch. Public traffic remains closed until the website BFF,
client-subject rate limiting, deployment configuration, and production gates
are complete.

## Audited boundaries

- internal demo session create/current;
- internal demo chat, reservation list, and reset;
- direct reservation API and its OpenAPI contract;
- validation, authentication, persistence, provider, timeout, and rate-limit
  errors;
- history, idempotent replay, structured mutation, handoff, reset, revoke,
  expiry, and cleanup;
- provider prompts/results, safe logging, transaction failures, and migration
  failures.

Internal demo routes remain absent from OpenAPI and require the server-only BFF
service credential. Their responses use explicit DTO allowlists and
`Cache-Control: no-store`.

## Final hardening

The complete demo core turn now runs under one 30-second overall deadline. The
deadline covers the provider-backed orchestration and immediate typed result
validation; it is not restarted between classifier, planner, workflow, or
general-response phases. A timeout cancels the in-flight turn, returns only the
stable provider-timeout signal, releases conversation/database locks, and
leaves at most the durable user request marker. A retry with the same request
key therefore cannot repeat an uncertain reservation mutation.

New assistant content is canonicalized and structurally validated before it is
persisted with safe-content provenance. Empty, control-bearing, or responses
over 4,096 Unicode code points fail closed without an assistant completion.

Internal handoff references are no longer serialized by session or chat DTOs.
The BFF receives only allowlisted simulated status, reason, fixed summary, and
timestamp fields where applicable. Persistent handoff identity remains internal
for isolation and cleanup.

## Boundary proof

The audited demo responses do not serialize raw reservation database IDs,
customer/owner/session IDs, token digests, request keys, service/session tokens,
database URLs, SQL, provider details, Telegram identities, or internal handoff
references. Phase D provenance checks prevent unmarked legacy assistant content
from history or replay; controlled reset is the recovery path.

Reservation mutation is derived only from the typed agent result and uses the
opaque canonical reservation reference. Duplicate replay reads the durable
reply/mutation/provenance and does not invoke the core again. Persistence and
migration exceptions use fixed safe envelopes and never reflect connection or
row details. Logs contain aggregate status/code fields rather than demo
identifiers or message content.

## Deployment blocker: client subject

The current create-session limiter is global and does not yet distinguish
public clients. This is acceptable only while public traffic remains closed.
Before go-live, implement one coordinated AURA/website change with this exact
shape:

1. The website accepts a platform-provided client address only when the chosen
   deployment runtime and trusted-proxy chain are explicitly configured.
2. The website canonicalizes the address with an IP parser; browser-supplied
   forwarding headers are never trusted directly.
3. The website computes a versioned HMAC subject using a dedicated server-only
   key and never stores or logs the address.
4. The website sends only the lowercase fixed-length subject digest to AURA on
   session creation.
5. AURA accepts that header only behind successful BFF service authentication,
   validates its exact shape, and charges a client-scoped create-session bucket
   in the same atomic rate-limit attempt as the global bucket.
6. Tests must prove malformed/missing subjects fail closed, raw addresses never
   enter persistence/logs/errors, spoofed forwarding headers have no effect,
   and concurrent requests cannot bypass either limit.

Required secret names and actual deployment proxy rules must be selected only
at the defined deployment human gates. No secret value belongs in source,
controller state, reports, or chat.

## Remaining integration work

The existing website session parser still expects the removed internal handoff
reference. It must be updated as part of the later BFF contract/integration
milestone before the backend change is deployed. Chat BFF remains blocked until
that audited contract is merged.
