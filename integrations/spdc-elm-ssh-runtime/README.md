# Alternative runtime: spdc-elm SSH, event ledger, and migrations

This branch is a **choice**, not a second schema beside the integration `SCHEMA` string.

Upstream: `spdc-elm/Cairn` `main`.

That fork replaces SQLite bootstrap with versioned migrations (`cairn/server/schema.py` plus `cairn/server/migrations`), replaces `ContainerManager` with an environment layer (Docker and SSH), and adds an execution event ledger, worker Q&A, and report tasks. The SSH environment imports `SshEnvironmentConfig`, which is not on the integration `DispatchConfig`. The migration runner imports `cairn.server.schema`, which would replace the current `db.SCHEMA` used by verify, research, CTF, and auth.

Those modules are vendored here under `integrations/spdc-elm-ssh-runtime/` and are **not** on the Python import path. They are not registered in `cairn.server.app`. Merging this pull request does **not** switch the running server or dispatcher. It keeps the integration stack intact and gives you the upstream runtime to adopt in a follow-up that replaces `SCHEMA`, `ContainerManager`, and `DispatcherLoop`.

## What you would give up by switching

Verify facts, research `project_kind`, CTF tables, admin-token worker routes, per-project local/docker overrides, and the auth tables are all migrations on the current `SCHEMA` string. The spdc migration chain is a different database. Adopting it means re-porting those tables or dropping them.

Goal Gate (tu95) is a different replacement of the same scheduler. Do not adopt both.

## Recommendation

Keep the integration branch's `DispatcherLoop`, `SCHEMA` string, and container/local execution. Use this branch only if you want SSH workers and a versioned execution ledger enough to replace that stack and re-port the landed features.

## Vendored paths

- `cairn/src/cairn/dispatcher/runtime/environments/` (Docker and SSH)
- `event_sink.py`, report/question/healthcheck tasks
- `cairn/src/cairn/server/migrations/` and `schema.py`
- execution, environment, branch, and worker routers
- `docs/specs/v1-worker-environment-requirements.md`
- `docs/specs/v2-command-blackboard-requirements.md`
