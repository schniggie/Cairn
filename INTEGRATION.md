# High-value fork integration

This branch pulls selected work from public Cairn forks onto `schniggie/Cairn` `main` (upstream `oritera/Cairn` at `8e7e0ea`, docs: add logo). Each landed fork is its own commit so it can be reverted. English prompts and the main README stay the defaults. Chinese text is an extra `zh-CN` prompt group and packaging notes.

`uv run --group dev pytest -q` from `cairn/`: **399 passed, 2 skipped**.

Not verified here: Docker image builds, GHCR publish, browser UI, Playwright browser install, live CTF platforms, and a host `claude` binary for the research runtime status test.

## Checklist

| Source | Status | Notes |
| --- | --- | --- |
| timwhitez/Cairn@main | Landed | Bounded runtime controls |
| Theearthwormsplitsvertically/Cairn@main | Partial | Pi safety is opt-in; other workers stay |
| superZhao913/Cairn@fix/pass-worker-prompts-via-stdin | Landed | Claude and Codex prompts go through stdin |
| wuerror/Cairn@main | Deferred | Verify phase rewrites shared server and scheduler code |
| LING12138-sg/Cairn@main | Landed | Intent failure, difficulty routing, commander scripts, worker tools |
| serein431/Cairn@feat/evolve-integration | Deferred | Eval hooks overlap the worker execution path |
| yux1azhengye/Cairn@main | Partial | SSE, HTTP evidence, config API; static page not merged |
| Shennnnnnnnnn/Cairn@main | Partial | Gemini CLI adapter only |
| Nicholas1126/Cairn@main | Deferred | Would replace the execution engine; FlockOS excluded |
| spdc-elm/Cairn@main | Deferred | SSH runtime and migrations rewrite the server |
| tu95/Cairn@experiment/cairn-less-is-more-v1 | Deferred | Goal Gate replaces the scheduler protocol |
| Rinne666/Cairn@main | Deferred | Auth control plane rewrites db, scheduler, and local process |
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

**Deferred.** The verify / verify_conclude task, harness, and vuln YAML examples are a large rewrite of `services.py`, models, intents, projects, prompts, and the scheduler (about 8k lines, and the branch is 6 commits behind upstream). That overlaps safety prompts, CTF intent fields, and the current task runner. A blind merge would break the suite. Revisit as its own branch after the execution path settles.

## 5. LING12138-sg/Cairn

**Landed.** Intent `concluded_as` and `retry_count`, `POST /projects/{id}/intents/{id}/fail`, project `difficulty`, optional worker `difficulties`, stale/dead retry thresholds, English CTF guidance added to the default prompts, commander scripts under `scripts/`, and worker-image packages for gdb, angr, pwntools, unicorn, and Ghidra 12.1.3.

**Skipped.** Rewriting apt sources to `kali.download`, and bumping the Claude/Codex/Pi npm versions. Those are regional or unrelated to the toolkit.

**Review.** A worker with `difficulties` set is skipped when the project difficulty is missing or not in that list. Dead bootstrap intents schedule a reason task with trigger `bootstrap_dead`. The Ghidra URL is pinned in the Dockerfile and was not built in this environment. Commander scripts were scanned for committed secrets; none were found.

## 6. serein431/Cairn

**Deferred.** The benchmark adapters (`benchmarks/`) and `cairn_runs/` scripts are new, but the useful behavior (admin token, `init_files`, trajectory extraction, session logs before container cleanup) is woven through config, the protocol client, containers, every worker adapter, and the scheduler. That is the same path already changed for bounded output, stdin, and safety. Landing the scripts without those hooks would not actually stop cross-project flag leakage or save session logs.

## 7. yux1azhengye/Cairn

**Partial.** Runtime events with `GET /events/stream`, structured HTTP evidence detached from worker JSON, and a redacted `GET/PUT /dispatch-config` that hot-reloads worker settings when the dispatcher is idle. Server, execution mode, and container/local blocks still need a restart. The thread pool size was left at `runtime.max_workers` (the fork also raised it to at least 32).

**Skipped.** The static admin editor in `index.html`. That page already carries the audit UI, and the fork's HTML rewrite does not apply cleanly on top of it. Codex `--skip-git-repo-check` was not taken.

**Review.** `cairn serve --dispatch-config` points the editor at a YAML file. Secrets whose key contains KEY, TOKEN, SECRET, PASSWORD, or AUTH are masked as `********` and restored on save.

## 8. Shennnnnnnnnn/Cairn

**Partial.** Gemini CLI worker type (`gemini`). It uses host authentication, optional `GEMINI_MODEL`, `--yolo -p`, and `supports_conclude() == False`. Health checks do not ping a remote API; they report the host CLI as the auth source. The same driver object is used for container and local mode.

**Skipped.** Local-runner mode is already on upstream. Also skipped: project summaries, per-project workdirs, `start.sh`, `dispatch.yaml` from the fork (may contain local config), `uv.lock` churn, and the `zh` prompt group. Chinese prompts landed later from XVSHIFU as `zh-CN`, with the safety placeholders the current validator requires.

## 9. Nicholas1126/Cairn

**Deferred.** The branch is about 100 commits and +260k lines. It vendors a FlockOS / `flock` tree, which was excluded. The Cairn-only pieces (opencode, a second local engine, in-app chat, skills CRUD, project knowledge) replace `dispatcher/runtime` rather than sit beside `local_backend` / `LocalProcess`. Taking both engines would fork process handling, stdin, and bounded output.

## 10. spdc-elm/Cairn

**Deferred.** SSH environments, the execution ledger, interactive Q&A, versioned migrations, and report tasks are a second server schema and runtime (about 46 commits). They collide with the current `db.py` schema, scheduler, and container manager. Opsx command packs are markdown around that runtime, so they were not copied alone.

## 11. tu95/Cairn

**Deferred.** Goal Gate and IntentRun replace dispatcher design, intake, and the server protocol. Kali skills and `start.py` assume the Worker-in-Kali runtime and a rewritten scheduler. That cannot sit beside safety injection, difficulty routing, and the current explore/bootstrap tasks without a broken dispatcher.

## 12. Rinne666/Cairn

**Deferred.** Playwright storage-state auth, the auth control plane, and the desktop helper are about 15k lines and rewrite models, db, the scheduler, local process, prompts, and the worker image. The Windows local-worker fixes live inside that same rewrite, so they were not split out. Chrome DevTools in the image was left with the existing worker Dockerfile plus the LING toolkit packages.

## 13. XVSHIFU/Cairn `feat/ctf-dasctf-integration`

**Partial.** DasCTF and CTFd adapters, `cairn ctf-bridge`, `/ctf` config and challenge API, tests, `zh-CN` prompts, `packaging/zh-CN`, and `scripts/llm_proxy.py`. Conclude prompts in `zh-CN` gained `{safety_decision_context}` so `prompt_group: zh-CN` validates.

**Skipped.** `dist/*.zip` build artifacts. The English README was not replaced. `index.html` was not swapped for the Chinese CTF dashboard (about +1200 lines on top of the audit UI). The zh release workflow no longer passes `--latest`, so a `vX.Y.Z-zh` tag does not steal the repository's latest release.

**Review.** CTF tokens and model API keys are stored in sqlite. `GET /ctf/config` masks them; `?full=true` returns the raw values. The bridge is a separate process from the dispatcher.

## 14. XVSHIFU/Cairn `feat/research-workbench`

**Partial.** Research sessions, reports, identities (AES-GCM via `cryptography`), source snapshots and compare, sandboxed worker (`cairn research-worker`), egress preload, and `/research` static UI. Research rows use `projects.project_kind = 'research'` and are hidden from `GET /projects`. Claude analysis metadata parsing was added so the worker can read the CLI JSON envelope. Non-Claude research drivers pass prompt stdin through when the driver uses it.

**Skipped.** The vulnerability-mining schema and campaign tables that still sit in that branch's `db.py` (hundreds of `vuln_` references). Core dispatcher, container, and adapter rewrites from the mining platform. `playwright` is imported only inside the capture helper and is not a required dependency; install it and the Chromium browser before using web capture. Vendored chart code is `cairn/src/cairn/server/static/vendor/echarts.min.js`.

**Review.** The worker refuses to run without `bwrap`. Runtime `available` is true only when `claude` is on `PATH` and a worker heartbeat is fresh. `test_runtime_reflects_live_worker` skips when `claude` is absent. Identities need the cryptography package (locked in `cairn/uv.lock`).

## Owner decisions

- Enable Pi safety by default or leave it opt-in.
- Whether the yux config editor and the XVSHIFU CTF dashboard should be merged into `index.html` or stay API-only.
- Whether research web capture should become a required `playwright` dependency.
- Whether to schedule a follow-up for verify-phase (wuerror), benchmark auth (serein), or the auth control plane (Rinne666) on top of this stack.
- Ghidra in the worker image increases build time and image size; the download URL can move.
