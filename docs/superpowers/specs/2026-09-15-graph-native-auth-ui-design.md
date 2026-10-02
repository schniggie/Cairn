# Graph-native Authentication UI Design

**Goal:** Surface the existing authentication lifecycle inside Cairn’s Project graph, details panel, timeline, and project settings without exposing secrets or creating a separate authentication product.

## Scope and compatibility

This design supersedes the older deferral of authentication Web UI work to a later version. It retains the existing AuthRequest state machine, Auth Helper, interactive browser capture, AuthSession facts, and fixed three task types. AuthRequest remains coordination state, not a Fact or Intent; the UI renders it as a virtual graph node.

P0 excludes sessionStorage, persistent browser contexts, session freshness policies, WebAuthn, credential management, and remote browser control.

## Required protocol-alignment phase

Before exposing authentication in the Web UI, align the existing implementation with its own protocol rules.

- Dispatcher remains the only writer of graph and AuthRequest protocol state. Auth Helper becomes a local, non-secret event producer: it receives a request id, performs local browser work, and emits a constrained completion event for Dispatcher consumption. It no longer directly claims, transitions, creates intents, or concludes Facts through `CairnClient`.
- Dispatcher always derives `role` and `login_url` from the configured `AuthTargetConfig`; LLM-provided URLs are ignored. Server APIs never expose a raw failure reason and use a fixed allowlisted failure summary.
- Persist AuthRequest lifecycle events with explicit timestamps: created, claimed, browser-opened, waiting, verifying, completed, failed, cancelled, expired. Persist a `resumed` event only when Dispatcher schedules a dependent Explore task; it must not be inferred from auth completion.

This phase is a prerequisite for UI work because it establishes safe UI data, timeline accuracy, and the mandated writer boundary.

### AuthEvent contract

`auth_events` is an append-only control-plane queue, separate from Facts, Intents, and AuthRequests. Its additive SQLite migration stores `id`, `project_id`, `request_id`, `kind`, `idempotency_key`, `actor_id`, safe metadata, timestamps, and processing outcome. Valid Helper-originated kinds are `launch_requested`, `browser_opened`, `login_succeeded`, and `login_failed`; UI-originated operations enqueue restricted commands. Browser JavaScript never transitions an AuthRequest, launches a browser, or submits an AuthEvent. A unique `(actor_id, idempotency_key)` constraint makes retries safe. The Server validates project/request association and persists events but does not make business transitions. Dispatcher polls unprocessed events, validates configured target metadata, applies the only allowed state transition, writes graph results, and marks outcome through its internal control-plane RPC.

Helpers authenticate event submission with a configured per-helper bearer secret held only in their local execution environment. The event API never accepts session material, URL overrides, role overrides, raw browser errors, or arbitrary payload text. Remote Helpers use the same HTTPS event API; local deep links only activate the installed local Helper and do not require a colocated Dispatcher.

### Safe UI projection contract

`GET /projects/{project_id}/auth-requests` returns a dedicated `AuthRequestView`, not the raw `AuthRequest`: `id`, `project_id`, `source_fact_ids`, `auth_ref`, `role`, fixed request summary, `status`, lifecycle timestamps, and fixed terminal summary. It omits `login_url`, raw reason, raw failure reason, helper identity, event metadata, and all AuthStore material. The global operator API remains Helper-only and is not a browser UI source. Tests must prove forbidden fields and secret-bearing values never serialize.

## Data and API design

Add a project-scoped metadata-only read endpoint, `GET /projects/{project_id}/auth-requests`, returning requests for that project after normal TTL reaping. It returns id, source_fact_ids, configured auth_ref and role, a safe request summary, status, and lifecycle timestamps/events. It never returns login URLs, AuthStore paths, raw failure reasons, or state files.

Add project-scoped actions only where the server can safely coordinate state:

- `POST /auth-commands` queues `cancel(request_id)` or `reauthenticate(auth_ref)` with an authenticated operator identity and idempotency key. Dispatcher validates its configured target before creating/reusing a request. There are no direct cancel/reauthenticate transition routes and no UI-supplied role or URL.

The initial UI action is deliberately not a Server "launch browser" endpoint. `Open Login` navigates to `cairn://auth/<request_id>`; a registered local Helper opens a local IPC connection to Dispatcher, which validates the request and authorizes its browser action. The protocol handler is registered by `cairn auth-helper install-uri-handler` and tested separately. The UI cannot reliably detect OS registration, so it displays a fixed local-helper instruction after navigation rather than claiming the handler is unavailable.

## UI design

The existing Alpine/Cytoscape single-page frontend remains unchanged in technology and visual language.

- Build Cytoscape elements from facts, intents, and active AuthRequests. An AuthRequest is a virtual `auth_required` node, connected from `source_fact_ids`, styled from existing amber/blue/red palettes according to `pending`, `claimed`, `waiting_user`, `verifying`, `failed`, `expired`, or `cancelled`.
- Render AuthSessionVerified and AuthSessionInvalid as semantic Fact labels/details while preserving their normal Fact storage and graph edges.
- Extend the existing Details tab with an Authentication section: target, role, source facts, fixed `authentication_required` request summary, status, timestamps, allowlisted verification-method labels (`page`, `selector`, `http`), and status-appropriate Open Login / Retry / Cancel actions. No Cookie, token, storage path, raw state, raw reason, or raw verifier error is rendered.
- Extend the existing Timeline projection with AuthRequest lifecycle events and add a compact project-level action-required indicator. Poll project plus project-scoped AuthRequests on the existing refresh cadence; completed virtual nodes disappear while the resulting Fact remains.
- Add a metadata-only Authentication section to project settings, summarizing graph-derived `valid`, `invalid`, and `missing` states from AuthSession facts. It does not claim storage freshness, enumerate AuthStore files, or expose local-helper state.

## Runtime flow

```text
Reason → AuthRequest → virtual Auth Required node → cairn:// helper handoff
      → interactive login → AuthSessionVerified/Invalid Fact → graph refresh → explore continues
```

Dispatcher creates requests, consumes Helper events, and writes state transitions/Facts. The local Helper performs browser interaction only. The UI does not write secrets and does not replace Dispatcher scheduling.

## Error handling and security

The UI maps backend state directly and displays only fixed safe failure messages. Active requests remain selectable until a terminal state; terminal requests leave the graph but remain in Timeline. Retry calls reauthenticate to create/reuse a new request. API serializers and frontend rendering remain metadata-only; server-side validation rejects invalid state transitions. Poll active auth requests at one second while they exist, returning to the current normal cadence when none exist.

## Verification

- API tests: project isolation, TTL reaping, state transitions, and metadata-only serialization.
- Static UI tests: virtual node mapping, details actions, timeline events, deep-link fallback, and no secret-bearing fields.
- Existing full pytest suite remains green; manually inspect the Graph with fixture data for visual consistency.
