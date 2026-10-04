# High-value fork integration

This branch pulls selected work from public Cairn forks onto `schniggie/Cairn` `main` (upstream `oritera/Cairn` at `8e7e0ea`, docs: add logo). Each landed fork is its own commit so it can be reverted. English prompts and the main README stay the defaults. Chinese text is an extra `zh-CN` prompt group and packaging notes.

`uv run --group dev pytest -q` from `cairn/`: **598 passed, 2 skipped**.

Not verified here: Docker image builds, GHCR publish, browser UI, Playwright browser install, live CTF platforms, and a host `claude` binary for the research runtime status test.

## Checklist

| Source | Status | Notes |
| --- | --- | --- |
| timwhitez/Cairn@main | Landed | Bounded runtime controls |
| Theearthwormsplitsvertically/Cairn@main | Partial | Pi safety is opt-in; other workers stay |
| superZhao913/Cairn@fix/pass-worker-prompts-via-stdin | Landed | Claude and Codex prompts go through stdin |
| wuerror/Cairn@main | Landed | Verify phase, observations, and allowlist harness added beside the current conclude path |
| LING12138-sg/Cairn@main | Landed | Intent failure, difficulty routing, commander scripts, worker tools |
| serein431/Cairn@feat/evolve-integration | Landed | Admin token, init files, trajectories, session logs |
| yux1azhengye/Cairn@main | Partial | SSE, HTTP evidence, config API; static page not merged |
| Shennnnnnnnnn/Cairn@main | Partial | Gemini CLI adapter only |
| Nicholas1126/Cairn@main | Partial | OpenCode, skills, per-project engine selection. FlockOS excluded |
| spdc-elm/Cairn@main | Separate PR | SSH runtime, ledger, and migrations are mutually exclusive with this schema |
| tu95/Cairn@experiment/cairn-less-is-more-v1 | Partial | Kali skills landed. Goal Gate is a separate PR and is not wired |
| Rinne666/Cairn@main | Landed | Auth control plane in legacy mode. Current scheduler and schema stay |
| XVSHIFU/Cairn@feat/ctf-dasctf-integration | Partial | Bridge and `/ctf` API landed; dashboard UI did not |
| XVSHIFU/Cairn@feat/research-workbench | Partial | Workbench landed; vulnerability-mining schema did not |

## 1. timwhitez/Cairn

**Landed.** Project and lease deadlines, bounded worker stdout/stderr, heartbeat-failure handling, and provider-neutral Pi settings (`--thinking` / `PI_REASONING_EFFORT`).

Ten commits were squashed into one after a clean apply onto current main. The only upstream commit this fork lacked was the logo.

## 2. Theearthwormsplitsvertically/Cairn

**Partial.** Kept the safety policy engine, audit store, `/audit` router, resource budgets, Pi TypeScript extension, CI test job, and lowercase GHCR owner tagging.

**Skipped on purpose.** The fork deleted Claude, Codex, and mock, required safety for every container run, gutted `docs/specs/dispatcher-design.md`, removed Claude and Codex from the worker image, and hardcoded a GHCR namespace. Those changes do not coexist with the rest of this fork. Safety is optional: omit the `safety` block, or set `enabled: false`. Only Pi loads the extension. `docker-compose` no longer requires `CAIRN_SAFETY_TOKEN` when safety is off.

**Review.** Turn safety on only after `CAIRN_SAFETY_TOKEN` is set. The Pi extension tests need Node with `--experimental-strip-types`.

## 3. superZhao913/Cairn

**Landed.** Claude and Codex worker prompts are passed on stdin so large prompts are not subject to `argv` / `execve` limits. Codex uses `-- -`. Pi still passes the prompt with `-p`, matching the source commit. Local execution got the same stdin path, not only containers.

## 4. wuerror/Cairn

**Landed, adapted.** Verify is an extra task type. Facts can carry type, confidence, locations, evidence, and payload drafts. `POST .../conclude` still accepts a single `description`. It also accepts `observations`, which insert one fact per observation. Base knowledge, fire approval, verify kill switch, and proxy traffic are new tables and routes. The dispatcher-owned harness (`execute_allowed_request`) fires only after allowlist checks. Explore still requires a `description` payload. The codebase bind is applied through `ensure_static_container` on bootstrap and explore.

**Skipped.** Replacing `validate_explore_payload` with an observations parser. That would break the current explore tests. The P3 explore end-to-end test was narrowed to the codebase bind plus a description payload.

## 5. LING12138-sg/Cairn

**Landed.** Intent `concluded_as` and `retry_count`, `POST /projects/{id}/intents/{id}/fail`, project `difficulty`, optional worker `difficulties`, stale/dead retry thresholds, English CTF guidance added to the default prompts, commander scripts under `scripts/`, and worker-image packages for gdb, angr, pwntools, unicorn, and Ghidra 12.1.3.

**Skipped.** Rewriting apt sources to `kali.download`, and bumping the Claude/Codex/Pi npm versions. Those are regional or unrelated to the toolkit.

**Review.** A worker with `difficulties` set is skipped when the project difficulty is missing or not in that list. Dead bootstrap intents schedule a reason task with trigger `bootstrap_dead`. The Ghidra URL is pinned in the Dockerfile and was not built in this environment. Commander scripts were scanned for committed secrets; none were found.

## 6. serein431/Cairn

**Landed.** Optional `CAIRN_ADMIN_TOKEN` guards `/projects`, `/skills`, and `/engines`. Worker write paths (heartbeat, claim, release, conclude, facts, hints, fail, events, http-records) stay open. `init_files` are injected into the project workspace. Claude, Codex, and Pi trajectory extractors plus session-log copy-out run before cleanup. Optional `WorkerConfig.model` is passed only when set.

**Skipped.** Their `LocalProcessManager` switch, emptying `WORKER_ENV_KEYS`, and a `DriverResult` without assets/stdin. The evolve scripts import an external flywheel that is not in this repo. The hardcoded batch token was removed; scripts read `CAIRN_ADMIN_TOKEN`.

## 7. yux1azhengye/Cairn

**Partial.** Runtime events with `GET /events/stream`, structured HTTP evidence detached from worker JSON, and a redacted `GET/PUT /dispatch-config` that hot-reloads worker settings when the dispatcher is idle. Server, execution mode, and container/local blocks still need a restart. The thread pool size was left at `runtime.max_workers` (the fork also raised it to at least 32).

**Skipped.** The static admin editor in `index.html`. That page already carries the audit UI, and the fork's HTML rewrite does not apply cleanly on top of it. Codex `--skip-git-repo-check` was not taken.

**Review.** `cairn serve --dispatch-config` points the editor at a YAML file. Secrets whose key contains KEY, TOKEN, SECRET, PASSWORD, or AUTH are masked as `********` and restored on save.

## 8. Shennnnnnnnnn/Cairn

**Partial.** Gemini CLI worker type (`gemini`). It uses host authentication, optional `GEMINI_MODEL`, `--yolo -p`, and `supports_conclude() == False`. Health checks do not ping a remote API; they report the host CLI as the auth source. The same driver object is used for container and local mode.

**Skipped.** Local-runner mode is already on upstream. Also skipped: project summaries, per-project workdirs, `start.sh`, `dispatch.yaml` from the fork (may contain local config), `uv.lock` churn, and the `zh` prompt group. Chinese prompts landed later from XVSHIFU as `zh-CN`, with the safety placeholders the current validator requires.

## 9. Nicholas1126/Cairn

**Partial.** OpenCode worker (`opencode run`, container mode uses `OPENCODE_CONFIG_CONTENT` when the model env is set). Skills store at `~/.cairn/skills` with zip upload as a raw body (no python-multipart). Per-project `backend` selects the existing `LocalBackend` or `ContainerManager`. `project_root` and `origin.codebase.path` are host mounts: creating a project with either requires the admin bearer, and both backends refuse the mount unless the path is a real non-symlink directory inside `CAIRN_PROJECT_SOURCE_ROOT` (falling back to `CAIRN_RESEARCH_SOURCE_ROOT`). `/`, the home directory, and sensitive system directories are rejected even if the root is too wide. Bootstrap, explore, and reason prompts accept `{skills}` and `{project_knowledge}`.

**Skipped.** Vendored FlockOS / `flock`. Replacing `runtime/local` and `LocalProcess`. Chat, executions, and `index.html`. Host binary path overrides live in `~/.cairn/engines.json` and do not add a second scheduler.

## 10. spdc-elm/Cairn

**Separate PR, not wired.** SSH environments, the execution ledger, interactive Q&A, versioned migrations, and report tasks replace `SCHEMA`, `ContainerManager`, and the dispatcher. That cannot be merged into this branch without dropping verify, research, CTF, and auth tables.

The upstream modules are on branch `cursor/spdc-elm-ssh-runtime-1070` under `integrations/spdc-elm-ssh-runtime/`. They are not imported by the running server. See that branch's README. Opsx command packs were not copied; they assume the replaced runtime.

## 11. tu95/Cairn

**Partial.** Kali worker skills under `container/.agents/skills` are copied into the image and mentioned from the explore prompt. That does not need a new scheduler.

**Separate PR, not wired.** Goal Gate / IntentRun replaces `DispatcherLoop` and the conclude protocol. The upstream modules are on branch `cursor/tu95-goal-gate-1070` under `integrations/tu95-goal-gate/`. They are not imported. `start.py` stays in that snapshot. Do not adopt Goal Gate and the spdc migration chain together; each replaces the same scheduler.

## 12. Rinne666/Cairn

**Landed, adapted.** Auth requests, events, credentials, graph outbox, helper views, and deployment bootstrap are additive tables (`_ensure_auth_tables`). The `SCHEMA` string was not replaced. Default `auth_control_plane_mode` is `legacy`, so the dispatcher does not require a server token unless `auth` is configured. Reason can emit `auth` interventions when `auth` is set. Windows local-process process-group and `python3` resolution run only when `os.name == "nt"`. `cairn auth` and `cairn auth-helper` are extra CLI commands.

**Skipped.** Replacing the scheduler, local process, or prompts wholesale. The graph-native auth UI was not merged into `index.html`. `container/chrome-devtools-wrapper.sh` is in the tree. The Dockerfile does not install `chrome-devtools-mcp` or rewrite the image entrypoint, because that needs a network npm install and a build that was not run. Playwright stays a lazy import. Auth browser tests use a stand-in error class so collection does not require the package.

## 13. XVSHIFU/Cairn `feat/ctf-dasctf-integration`

**Partial.** DasCTF and CTFd adapters, `cairn ctf-bridge`, `/ctf` config and challenge API, tests, `zh-CN` prompts, `packaging/zh-CN`, and `scripts/llm_proxy.py`. Conclude prompts in `zh-CN` gained `{safety_decision_context}` so `prompt_group: zh-CN` validates.

**Skipped.** `dist/*.zip` build artifacts. The English README was not replaced. `index.html` was not swapped for the Chinese CTF dashboard (about +1200 lines on top of the audit UI). The zh release workflow no longer passes `--latest`, so a `vX.Y.Z-zh` tag does not steal the repository's latest release.

**Review.** CTF tokens and model API keys are stored in sqlite. `GET /ctf/config` always masks them. Raw values are only on `GET /ctf/internal/config`, which requires the admin bearer. The bridge is a separate process from the dispatcher.

Platform challenge text (description, target, attachments, hints) is wrapped as `UNTRUSTED PLATFORM DATA` before it enters Origin or Hints. Bootstrap, explore, reason, and verify prompts tell the worker that block is data, not instructions. Claude Code workers set `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` (also an `ENV` in `container/Dockerfile` for the pinned `@anthropic-ai/claude-code@2.1.98` image) so Bash, hook, and MCP stdio children do not inherit the provider credential. The parent `claude` process still has the token, because that is how it calls the model API.

## 14. XVSHIFU/Cairn `feat/research-workbench`

**Partial.** Research sessions, reports, identities (AES-GCM via `cryptography`), source snapshots and compare, sandboxed worker (`cairn research-worker`), egress preload, and `/research` static UI. Research rows use `projects.project_kind = 'research'` and are hidden from `GET /projects`. Claude analysis metadata parsing was added so the worker can read the CLI JSON envelope. Non-Claude research drivers pass prompt stdin through when the driver uses it.

**Skipped.** The vulnerability-mining schema and campaign tables that still sit in that branch's `db.py` (hundreds of `vuln_` references). Core dispatcher, container, and adapter rewrites from the mining platform. `playwright` is imported only inside the capture helper and is not a required dependency; install it and the Chromium browser before using web capture. Vendored chart code is `cairn/src/cairn/server/static/vendor/echarts.min.js`.

**Review.** The worker refuses to run without `bwrap`. Runtime `available` is true only when `claude` is on `PATH` and a worker heartbeat is fresh. `test_runtime_reflects_live_worker` skips when `claude` is absent. Identities need the cryptography package (locked in `cairn/uv.lock`).

## Owner decisions

### Which scheduler, schema, and runtime

**Recommendation: keep this branch.** `DispatcherLoop`, the current `SCHEMA` string, and container/local execution are the default. Safety, stdin, bounded output, CTF failure tracking, verify, research, the admin token, OpenCode, and the auth control plane all use that stack.

Pick **at most one** alternative. They replace the same core and were not forced together:

| Choice | Pull request | What changes if you adopt it |
| --- | --- | --- |
| Goal Gate / IntentRun | https://github.com/schniggie/Cairn/pull/3 (`cursor/tu95-goal-gate-1070`) | Scheduler and conclude protocol. Vendored, not wired. |
| SSH + event ledger + migrations | https://github.com/schniggie/Cairn/pull/2 (`cursor/spdc-elm-ssh-runtime-1070`) | `SCHEMA`, `ContainerManager`, and the dispatcher. Vendored, not wired. |

Merging either alternative PR only adds the upstream source under `integrations/`. It does not switch the running process. A real switch is a follow-up that replaces the core and re-ports the features above. I could not import those runtimes against this schema without that replacement.

### Other choices

- Pi safety stays opt-in.
- The yux config editor and the XVSHIFU CTF dashboard are still API-only. Their HTML was not merged into `index.html`.
- Research web capture and auth browser login stay optional. `playwright` is not a required dependency.
- Auth control plane defaults to `legacy`. Set `auth` and `auth_control_plane_mode` only when you want the helper and fire path. If `CAIRN_ADMIN_TOKEN` and `server_token` are both set, project calls use the admin token and `/auth` calls use `server_token`.
- Ghidra in the worker image increases build time and image size; the download URL can move.
- Chrome DevTools wrapper is not installed in the image until `chrome-devtools-mcp` is added to the Dockerfile.

## Known residual risk: CTF prompt injection and worker egress

Platform text is labeled untrusted and Claude Code 2.1.98 tool subprocesses scrub provider credentials, but this is not a complete containment boundary.

- A worker still runs with `--dangerously-skip-permissions`. A malicious challenge can still steer tool use. The scrub stops Anthropic and cloud-provider credentials from appearing in those tool subprocesses; it does not stop the model from sending other workspace data to a network destination.
- There is no server-side model proxy in the worker path, and worker egress is not allowlisted. Compose and dispatch examples can use `network_mode: host`. `scripts/llm_proxy.py` is not wired in front of worker traffic.
- Codex, Pi, OpenCode, and Gemini do not get `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`. Local execution merges the host environment into the worker process, so other host secrets can still be visible to that process.
- Prompt fences are a model instruction, not a sandbox. Treat external CTF platforms as untrusted input sources and keep provider credentials off hosts that run auto-approved workers until egress is restricted.
