# Alternative runtime: tu95 Goal Gate / IntentRun

This branch is a **choice**, not a second scheduler running beside `DispatcherLoop`.

Upstream: `tu95/Cairn` branch `experiment/cairn-less-is-more-v1`.

The Goal Gate / IntentRun runtime replaces the current dispatcher protocol. Its server module imports types that do not exist on the integration stack (`IntentRun`, `IntentRunClaimRequest`, `ContinueEvidence`, `CommittedHTTPException`, `encode_checkpoint_json`). Its worker path resumes from checkpoints and an intake task instead of the current bootstrap / reason / explore / verify loop.

Those modules are vendored here under `integrations/tu95-goal-gate/` and are **not** on the Python import path. They are not registered in `cairn.server.app`. Merging this pull request does **not** switch the running dispatcher. It keeps the integration stack's tests green and gives you the upstream runtime to adopt in a follow-up that replaces `DispatcherLoop` and the server schema.

## What you would give up by switching

The integration branch's safety injection, stdin prompts, bounded output, intent failure tracking, verify harness, research workbench, CTF bridge, admin token, OpenCode/skills, and the auth control plane all call the current scheduler and schema. Goal Gate does not include those hooks. Adopting it means re-porting them or dropping them.

Kali skills from this same fork are already on the integration branch (`container/.agents/skills`). They do not need Goal Gate.

## Recommendation

Keep `DispatcherLoop` plus the current `SCHEMA` string and container/local execution as the default. Use this branch only if you want to replace that scheduler with IntentRun and accept re-porting the features above.

## Vendored paths

- `cairn/src/cairn/server/intent_runtime.py`
- `cairn/src/cairn/dispatcher/runtime/intent_run.py`
- intake task, intake/intent-run routers, workspace helper
- `docs/specs/goal-gate-intent-runtime-development.md`
- `start.py` / `start.md`
- upstream tests (they do not collect from `cairn/tests` while they live here)
