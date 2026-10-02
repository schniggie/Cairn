# Authentication Control Plane Design

**Hard boundary:** Server stores protocol records; Dispatcher is the only actor allowed to advance authentication business state or write graph facts/intents; Helper/CLI submit authenticated events only; Web UI reads a safe projection and submits constrained commands only.

## Authority and deployment boundary

- **Server** persists requests, queues, lifecycle records and read projections. It exposes a Dispatcher-authenticated internal *apply RPC* which executes one SQLite `BEGIN IMMEDIATE` transaction for queue acknowledgement, request/lifecycle mutation and outbox insertion. It is a storage transaction executor, not a business decision-maker; it never reads AuthStore or graph data.
- **Dispatcher** is the sole business writer: it claims events through HTTP, validates config/identity/state, verifies AuthStore, decides the transition and asks Server's internal apply RPC to commit that decision. It schedules the sole TTL reaper call; Server executes its approved transition atomically.
- **Helper/CLI** owns interactive browser capture and is the only component writing a local `AuthStore`. It never imports `CairnClient` or `AuthGraphAdapter`; it submits an event after browser progress/capture and does not complete a request.
- **Dispatcher and Helper share the configured AuthStore namespace** per target. Local mode uses one absolute `auth.store_root`; container/distributed mode uses a deployment-mounted, access-controlled shared volume. Dispatcher is read-only and Helper read/write. Helper first writes state bytes to a temporary target-private file, fsyncs and atomically renames it; it then writes/renames a manifest `{request_id, auth_ref, actor_id, capture_generation, captured_at, state_sha256}`. Dispatcher reads manifest then state and compares SHA-256 before verification. An interrupted capture has no accepted manifest; a replaced state cannot match the manifest. Dispatcher accepts `login_succeeded` only when this binding matches event/request actor/configured target. If it cannot, the event is `store_unavailable` or `capture_mismatch`.
- **Web UI** has no direct transition route. It sees `AuthRequestView`; browser commands enqueue work and never contain a URL, role, verification input or secret.

`AuthTargetConfig` addressed by `auth_ref` is the only authority for role, login URL, profile, verification policy, TTL and graph descriptions. Event/command attempts to supply any of those values are `schema_rejected`.

## Canonical durable schema

`auth_events` is append-only: `id` (server-generated immutable opaque id), `project_id`, `request_id`, `auth_ref`, `kind`, `actor_id`, `idempotency_key`, `occurred_at`, `received_at`, `state`, `attempt_count`, `next_attempt_at`, `claimed_by`, `claim_expires_at`, `processed_at`, `outcome_code`, and nullable `capture_generation` (required only by `login_succeeded`). `(actor_id, idempotency_key)` is unique; the project/request/auth_ref relation is validated. `kind` is exactly `launch_requested|browser_opened|login_succeeded|login_failed`; state is exactly `queued|claimed|retryable|applied|rejected`.

The accepted event JSON is exactly `{project_id, request_id, auth_ref, kind, idempotency_key, occurred_at, capture_generation?}`. Unknown fields cause 422. `auth_requests` gains nullable `helper_actor_id`; `launch_requested` atomically binds it to the event actor and browser/success/failure events must match it, otherwise they are `rejected/not_request_owner`. Migration initializes existing nonterminal requests to null; only a pending, unbound request can bind, terminal requests never rebind, and an expired helper binding returns the request to pending/null only through Dispatcher recovery. There is no payload or free-text/URL/cookie/credential/browser-error/profile-path/role metadata column.

`auth_commands` has the same queue, lease and idempotency columns. Its `kind` is `cancel|reauthenticate`; it has exactly one selector: `request_id` for cancel, or `auth_ref` for reauthenticate. Both/neither is 422. Success returns only `{command_id, state: "queued"}`.

`auth_lifecycle_events` is append-only: `request_id`, non-null `event_id`, `sequence`, `kind`, `recorded_at`, enum-only `outcome_code`. Its canonical replay guard is unique `(request_id, event_id, kind)`. Request creation uses `create:<request_id>` (and migration backfill `backfill:create:<request_id>`); TTL uses `ttl:<request_id>:<expiry_generation>`. Thus SQLite NULL semantics cannot admit duplicate lifecycle records.

## Consumption, transitions, and expiry

Dispatcher asks the Server's internal queue RPC to run `BEGIN IMMEDIATE`, select the oldest eligible queued/retryable record, and update it to `claimed`, `claimed_by=dispatcher_instance_id`, `claim_expires_at=server_now+30s`, incrementing `attempt_count`. Only its unexpired claim can be applied by that same Dispatcher through the internal apply RPC. A Server lease-recovery RPC returns expired claims to `retryable` with exponential server-clock backoff; three attempts ends `rejected/retry_exhausted`. Applied/rejected records are replay no-ops. Reuse of an idempotency key with different immutable data returns 409; identical data returns the original record.

| Input | Required status | Dispatcher result |
|---|---|---|
| `launch_requested` | `pending` | `claimed` |
| `browser_opened` | `claimed` | `waiting_user` |
| `login_succeeded` | `waiting_user` or `verifying` when lifecycle is keyed to this event id | record/reuse `verifying`; read configured store, verify target, then `completed` + `AuthSessionVerified`, or `failed` + `AuthSessionInvalid` |
| `login_failed` | `claimed`, `waiting_user`, `verifying` | `failed` + `AuthSessionInvalid` |
| `cancel` | `pending`, `claimed`, `waiting_user`, `verifying` | `cancelled` |
| `reauthenticate(auth_ref)` | configured target | create/reuse one nonterminal target request |

Invalid ordering is terminal `rejected/invalid_transition` and leaves the request untouched. `expires_at` is set at create time from the Server setting `auth_request_ttl` (existing nonnegative integer; default 1800 seconds; zero disables expiry). `auth_claim_ttl` remains a legacy request-claim setting and is not used by queued event claims (which use the fixed 30-second lease above). Dispatcher schedules the Server internal expiry RPC, which atomically changes due nonterminal requests to `expired`, writes deterministic lifecycle, and produces `AuthSessionInvalid` only when invalidating a previously verified session. All clocks are server/Dispatcher UTC.

`login_succeeded` is resumable. The first application atomically records `verifying` with the event id before calling external verification. If Dispatcher crashes, the lease retry sees that same event id and status `verifying`, reuses the lifecycle record and runs verification again; any other event is rejected. Completion/failure, lifecycle insertion, queue acknowledgement and graph outbox insertion are one transaction. This prevents a `verifying` request from becoming stuck after a crash.

Graph effects are a transactional `auth_graph_outbox` record keyed by non-null `effect_key` (`auth-event:<event_id>` or `auth-ttl:<request_id>:<expiry_generation>`), with request id, intended fixed Fact kind, state `pending|intent_created|fact_created`, and separately unique `intent_source_key`/`fact_source_key`. The internal apply RPC inserts this outbox in the same transaction as request/lifecycle/queue acknowledgement. Dispatcher first calls idempotent `create_intent(source_key)`, records returned intent id using an internal outbox ack RPC, then idempotent `conclude_intent(source_key)` and records fact id. Server stores nullable unique `source_key` on auth-generated intents and facts and supplies lookup by source key. On a crash, Dispatcher resumes the recorded next step; the same source key returns the existing object instead of duplicating it. A retry cannot duplicate or lose graph records.

## Endpoint and credential contract

Bearer credentials are deployment configuration, never graph/projection data. Server stores only SHA-256 token digest, actor id, scopes, project allowlist, `not_before`, `expires_at`, and `replaced_by`. Rotation adds a bounded-overlap digest; revocation sets expiry to server-now. HTTPS is required except loopback. Every endpoint requires scope and project authorization.

### First-run deployment prerequisite

The internal deployment endpoint is intentionally authenticated and is not a credential-creation endpoint. Before `DispatcherLoop` can call `POST /internal/auth/deployment`, the Server operator must provision the Dispatcher token through the Server-only deployment environment or an equivalent Server startup bootstrap. Set `CAIRN_AUTH_DISPATCHER_TOKEN` for the Server process (or include the same value in its deployment snapshot), start or restart the Server, and verify that only the SHA-256 digest was persisted in `auth_credentials`. Configure the identical opaque value as the Dispatcher's `server_token` only after that seed exists. A first call without the Server-side seed must return `401`; adding an unauthenticated first-run route would violate the deployment boundary. The Server-side bootstrap is the only first-run path and must never be exposed as a public API or written to graph records.

Pre-migration credentials without deployment ownership are not inferred from actor, scope, or project metadata: custom helper rows can be indistinguishable from manually provisioned credentials. If an operator has inventoried and accepted the migration impact, the Server-only environment variable `CAIRN_AUTH_LEGACY_CREDENTIAL_CUTOVER` may be set to exactly `revoke` for one startup. The Server then expires every still-unowned credential, records the acknowledgement and revocation count in an audit row, and ignores repeated acknowledgements. Leaving it unset preserves manual credentials; the cutover is never carried in a Dispatcher snapshot or exposed as an API operation.

| Caller | Endpoint | Scope | Permission |
|---|---|---|---|
| Helper/CLI | `GET /projects/{project_id}/auth-requests/{request_id}/helper-view` | `helper.request.read` | id, auth_ref, configured login URL, status only |
| Helper/CLI | `POST /auth-events` | `helper.event.submit` | enqueue its project event |
| Dispatcher | internal claim/ack/reap | `dispatcher.auth.consume` | consume queues and mutate auth state |
| Browser UI | `GET /projects/{project_id}/auth-requests` | `ui.auth.read` | safe project projection |
| Browser UI | `POST /auth-commands` | `ui.command.submit` | queue permitted command |

Browser identity is an operator session from the deployment reverse proxy/session layer, mapped to explicit project `read`, `cancel`, `reauthenticate` permissions. Without an identity provider UI command ingress is disabled (503); no token is embedded in HTML. Cookie deployments require same-origin + CSRF defense; bearer clients use UUID idempotency keys. Authorization failures are opaque 403/404.

`AuthRequestView` is a strict allowlist: `id`, `project_id`, `source_fact_ids`, `auth_ref`, `role`, `request_summary`, `verification_methods`, `status`, `created_at`, `claimed_at`, `completed_at`, `terminal_summary`. `request_summary` is always fixed `authentication_required`; `verification_methods` is the config-derived, closed subset of `page|selector|http` and never a URL/selector. `terminal_summary` is nullable before terminal state and otherwise exactly one fixed enum: `verified`, `login_failed`, `verification_failed`, `cancelled`, or `expired`; it is mapped only from Dispatcher outcome codes, never raw `reason`/`failure_reason`. The view excludes `login_url`, `reason`, `failure_reason`, `helper_id`, `actor_id`, `event_id`, `state_path`, and all secret-shaped values. Serializer tests assert allowlist equality and adversarial redaction.

## Exact legacy migration

`auth_control_plane_mode` is a Pydantic Server/Dispatcher setting: `legacy|dual_write|enforced`. Upgrades default legacy; fresh installs may default enforced only after parity tests pass. Existing Server DB settings `auth_request_ttl` and `auth_claim_ttl` remain the sole authoritative TTL values during migration. Before startup parity checking, the deprecated Dispatcher `intervention.request_ttl` validation changes from `gt=0` to `ge=0`; thus it can represent Server `0` (expiry disabled). Dispatcher reads settings from `GET /settings` and refuses startup on mismatch. The config fields are then removed in the next major version; no request uses config TTL directly.

| Current raw route/caller | Replacement | Enforced behavior |
|---|---|---|
| `POST /projects/{project_id}/auth-requests` from reason task | Dispatcher internal create operation | external route unavailable |
| `GET /auth-requests` from Helper | scoped helper view/query | unavailable |
| `POST /auth-requests/{id}/claim|waiting|verifying|complete|fail` from Helper/CLI | `POST /auth-events`; Dispatcher consumes | unavailable |
| `CairnClient`/`AuthGraphAdapter` in `cli.py` | store capture then success/fail event | imports/calls removed |

In legacy mode current paths remain. In dual-write, migrated actors are event-only and legacy direct calls from them are rejected; legacy actors stay legacy-only, preventing races. One lifecycle application maps to one stable graph key. Existing rows receive one created lifecycle row and terminal rows one terminal lifecycle. Rollback stops consumption but deletes no append-only data or completed state. Enforced raw mutations return 410 migration code; routes are physically removed next major version.

## Delivery and verification

1. Guarded DB migrations for queue/lifecycle/outbox, `auth_requests.helper_actor_id`, nullable unique source keys, config/example/test coverage for control-plane mode and zero-compatible deprecated `intervention.request_ttl`, scoped ingress, queue claim/ack/reap, tests.
2. Dispatcher internal service + CLI/Helper migration away from direct `CairnClient`/`AuthGraphAdapter` writes.
3. Dual-write parity proof, then enforcement.
4. Safe projection, operator command endpoint, Timeline/graph rendering, then local `cairn://` bridge.

Tests cover migrations; scope/project isolation; idempotency collision/replay; claim crash/reclaim; retry exhaustion; every transition/TTL edge; absent shared store; target-field rejection; graph deduplication; legacy races; forbidden projection keys.
