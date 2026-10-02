# Authentication Control Plane Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Helper/CLI direct authentication protocol writes with a durable, Dispatcher-consumed event control plane, without yet adding UI or deep-link behavior.

**Architecture:** Server owns additive SQLite queue/lifecycle schema and constrained HTTP ingress; Dispatcher owns state decisions and graph effects. Phase 1 deliberately preserves legacy external routes for non-migrated callers, but only migrated Helper/CLI use events. Server mutation primitives are internal Dispatcher endpoints rather than shared-DB access.

**Tech Stack:** Python 3.12, FastAPI, SQLite, Pydantic v2, pytest, uv.

---

### Task 1: Durable event schema and constrained ingress

**Files:**
- Modify: `cairn/src/cairn/server/db.py`, `cairn/src/cairn/server/models.py`, `cairn/src/cairn/server/services.py`, `cairn/src/cairn/server/app.py`, `cairn/src/cairn/dispatcher/config.py`, `dispatch.example.yaml`, `dispatch.local.example.yaml`
- Create: `cairn/src/cairn/server/routers/auth_events.py`, `cairn/src/cairn/server/routers/auth_helper_views.py`
- Test: `cairn/tests/test_db_migrations.py`, `cairn/tests/test_auth_events.py`, `cairn/tests/test_config_and_adapters.py`

- [ ] **Step 1: Write failing tests** for guarded upgrades that add `auth_events`, `auth_lifecycle_events`, `auth_credentials`, `auth_requests.helper_actor_id`, `expires_at`, and `expiry_generation`; require backfill of exactly one synthetic created lifecycle row and one terminal lifecycle row for old terminal requests, with deterministic expiry initialization. Assert unknown event fields fail validation, project/request/auth-ref mismatch is rejected, identical `(actor_id, idempotency_key)` returns its existing event, and conflicting reuse returns 409. Add Server credential tests for SHA-256 digest lookup, current/previous rotation overlap, revocation, scope and project allowlist; tests must prove actor identity is derived from the bearer principal and never accepted from body data. Test the transport guard: bearer ingress accepts HTTPS/reverse-proxy marked requests or loopback HTTP, but rejects non-loopback cleartext.
- [ ] **Step 2: Run red tests** with `uv run --project cairn --group dev pytest cairn/tests/test_auth_events.py cairn/tests/test_db_migrations.py -q`; confirm failure is missing tables/routes/models.
- [ ] **Step 3: Implement minimal persistence, strict helper view and `POST /auth-events`**. Add an operator-provisioned Server credential table/service; provision helper and Dispatcher token digests through deployment-only environment/config bootstrap (never API or graph). Helper configuration supplies actor/scopes/project allowlist and its opaque token; Dispatcher configuration supplies a separate opaque token with only `dispatcher.auth.consume`, injected into `CairnClient`/`DispatcherLoop` as an Authorization header. Resolve bearer token digest to actor/scope/project allowlist, require `helper.event.submit`, and apply the HTTPS/loopback transport guard. Add scoped `GET /projects/{project_id}/auth-requests/helper-pending` and `GET /projects/{project_id}/auth-requests/{request_id}/helper-view` with a dedicated strict model (`id`, `auth_ref`, configured login URL, `status`); only `helper.request.read` actors may use them and redaction tests prove global raw listing is unavailable to a migrated helper. Event ingress accepts only `project_id`, `request_id`, `auth_ref`, closed `kind`, idempotency UUID, occurred time, and optional capture generation. Do not accept free text, credentials, URLs, role or browser errors. Keep all raw AuthRequest routes unchanged in this task.
- [ ] **Step 4: Run focused tests** and commit `feat: add auth event ingress`.

### Task 2: Dispatcher claim/apply primitives and state transitions

**Files:**
- Modify: `cairn/src/cairn/server/db.py`, `cairn/src/cairn/server/services.py`, `cairn/src/cairn/server/models.py`, `cairn/src/cairn/server/routers/settings.py`, `cairn/src/cairn/server/routers/auth_requests.py`, `cairn/src/cairn/dispatcher/config.py`, `dispatch.example.yaml`, `dispatch.local.example.yaml`, `cairn/src/cairn/dispatcher/protocol/client.py`, `cairn/src/cairn/dispatcher/scheduler/loop.py`, `cairn/src/cairn/dispatcher/tasks/reason.py`
- Create: `cairn/src/cairn/server/routers/auth_control.py`, `cairn/src/cairn/dispatcher/auth_control.py`
- Test: `cairn/tests/test_auth_control.py`, `cairn/tests/test_auth_ttl.py`

- [ ] **Step 1: Write failing tests** for atomic server-side claim lease, three-attempt rejection, actor binding on `launch_requested`, invalid transition no-op, lifecycle replay key uniqueness, and a Dispatcher-authenticated internal apply request. Include wiring tests that Dispatcher config token reaches `CairnClient` and only its token can call internal endpoints; unauthenticated/helper tokens receive denial. Add mode tests for `legacy|dual_write|enforced`, migrated-actor legacy-write rejection and no dual-write race. Add creation tests proving `expires_at` is calculated from Server `auth_request_ttl`, zero disables expiry, and each expiry lifecycle key is deterministic from request/generation. Add loop tests proving each polling cycle consumes eligible events, recovers expired leases, and reaps TTL; add a Reason test proving request creation calls the internal Dispatcher create operation rather than the raw public route.
- [ ] **Step 2: Run red tests** with `uv run --project cairn --group dev pytest cairn/tests/test_auth_control.py -q`; confirm the internal protocol client/service is absent.
- [ ] **Step 3: Implement control-plane mode/config and internal claim/apply/create/expiry endpoints**. `auth_control_plane_mode` is configured and tested as `legacy|dual_write|enforced`; deprecated dispatcher TTL config becomes `ge=0` during parity and must equal Server settings. Add explicit auth-request router/authz guarding: legacy permits existing callers, dual-write rejects raw writes for an event-migrated credential actor, enforced returns 410 for every raw mutation. Server executes SQLite transactions; Dispatcher selects legal transitions only (`pending→claimed→waiting_user→verifying→completed|failed`), binding all follow-up helper events to the original actor. Create requests with `expires_at`/generation from Server settings; invoke event claim/recovery/TTL through `DispatcherLoop.run()` before ordinary project scheduling. Move Reason request creation to the internal Dispatcher create operation. Do not write graph facts in this task.
- [ ] **Step 4: Run focused tests** and commit `feat: add dispatcher auth event control`.

### Task 3: AuthStore capture integrity and Helper/CLI event migration

**Files:**
- Modify: `cairn/src/cairn/auth/models.py`, `cairn/src/cairn/auth/store.py`, `cairn/src/cairn/auth_helper/client.py`, `cairn/src/cairn/auth_helper/daemon.py`, `cairn/src/cairn/cli.py`, `cairn/src/cairn/auth/graph.py`, `cairn/src/cairn/server/app.py`
- Test: `cairn/tests/test_auth_store.py`, `cairn/tests/test_auth_helper.py`, `cairn/tests/test_auth_browser.py`

- [ ] **Step 1: Write failing tests** that a captured state creates an atomic manifest model with request/target/actor/generation and SHA-256 digest using write-temp/fsync/rename order; tampering/replacement fails validation. Add explicit mismatch cases for manifest/event actor, request id, auth_ref, capture generation and configured target. Add CLI tests requiring `--request` for every graph-affecting login/verify success/failure path; requests omitted from `auth login`/`auth verify` are local-only capture/verification and never submit an event or write graph/state. Helper/CLI with a request emit events and no longer invoke `claim_auth_request`, state transition client methods, or `AuthGraphAdapter`. Add a safe, scoped pending-query/helper-view contract to replace global `GET /auth-requests`; it may expose request id, auth_ref, configured login URL and status to its own helper only, but no reason/failure/session data.
- [ ] **Step 2: Run red tests** with `uv run --project cairn --group dev pytest cairn/tests/test_auth_store.py cairn/tests/test_auth_helper.py cairn/tests/test_auth_browser.py -q`; confirm direct legacy writes are still observed.
- [ ] **Step 3: Implement manifest-aware storage, scoped helper discovery, and event-only callers**. Daemon submits `launch_requested`, polls its helper view until Dispatcher has applied `claimed` with its actor binding, and only then launches; it emits `browser_opened` after launch. CLI follows the same ordered acknowledge-before-follow-up sequence and uses request-scoped idempotency keys. Add an integration race test proving immediate follow-up is not launched/accepted before the claim acknowledgement. Preserve credentials/session state only in AuthStore; never include them in events, facts or prompts. Migrate `AuthHelperClient.list_pending()` to the scoped helper query/view. Delete dead direct-write code rather than leaving an alternate path for migrated callers.
- [ ] **Step 4: Run focused tests** and commit `refactor: migrate auth helpers to events`.

### Task 4: Dispatcher verification, graph outbox, and end-to-end regression

**Files:**
- Modify: `cairn/src/cairn/dispatcher/auth_control.py`, `cairn/src/cairn/server/db.py`, `cairn/src/cairn/server/services.py`, `cairn/src/cairn/server/models.py`, `cairn/src/cairn/server/routers/auth_control.py`, `cairn/src/cairn/server/routers/intents.py`, `cairn/src/cairn/server/routers/projects.py`, `cairn/src/cairn/dispatcher/protocol/client.py`, `cairn/src/cairn/auth/graph.py`
- Test: `cairn/tests/test_auth_control.py`, `cairn/tests/test_auth_graph.py`, `cairn/tests/test_auth_intervention.py`

- [ ] **Step 1: Write failing tests** for resumable `login_succeeded` verification by the same event, unavailable/tampered store rejection, exactly-once outbox intent/fact creation across simulated crashes, sanitized `AuthSessionInvalid` failure facts, and proof that a Helper credential cannot invoke source-key graph creation/conclusion or outbox acknowledgement.
- [ ] **Step 2: Run red tests** with `uv run --project cairn --group dev pytest cairn/tests/test_auth_control.py cairn/tests/test_auth_graph.py -q`; confirm no outbox/source-key recovery exists.
- [ ] **Step 3: Implement minimal durable outbox and idempotent internal graph RPCs**. Add guarded nullable unique source keys for auth-generated intents/facts. Expose source-key lookup/create/conclude and outbox acknowledgement only beneath `auth_control.py` internal endpoints requiring `dispatcher.auth.consume`; never add source keys to public graph routes. Persist each event's pending/intent-created/fact-created progress; reuse source keys after every simulated crash; complete/reject only through Dispatcher control service.
- [ ] **Step 4: Run focused tests, then the complete suite** with `uv run --project cairn --group dev pytest`; commit `feat: complete dispatcher auth control plane`.

### Task 5: Protocol review and Phase 2 handoff

**Files:**
- Modify: `docs/superpowers/specs/2026-09-15-authentication-control-plane-design.md`, `docs/superpowers/plans/2026-09-15-graph-native-auth-ui.md`
- Test: full `pytest` suite

- [ ] **Step 1: Verify implementation against the control-plane spec**: no Helper/CLI graph or AuthRequest write, no raw secret in accepted event/graph fact, all transitions Dispatcher-mediated, and legacy raw routes still explicitly isolated.
- [ ] **Step 2: Run `uv run --project cairn --group dev pytest`**; confirm all tests pass.
- [ ] **Step 3: Record Phase 1 status and make a separate reviewed Phase 2 UI/deep-link plan**; do not start UI in this task.
