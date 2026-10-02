# Authentication P0 Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the existing non-protocol browser-auth correctness gaps before adding browser automation or automatic login.

**Architecture:** Preserve the current human-interactive capture flow and three fixed task types. Strengthen its runtime behavior in focused components: capture honors configured IndexedDB persistence; verifier faults become invalid verification results; auth-helper capacity is reclaimed after child exit; and reason only emits configured, enabled auth interventions. No graph schema, task contract, or credential flow changes occur in this phase.

**Tech Stack:** Python 3.12+, Pydantic v2, Playwright sync API, pytest.

---

### Task 1: Durable capture and fault-contained verification

**Files:**
- Modify: `cairn/src/cairn/auth/manager.py`
- Modify: `cairn/src/cairn/auth/verifier.py`
- Test: `cairn/tests/test_auth_*.py` (extend the existing focused file(s))

- [x] Write regression tests proving configured `indexed_db` reaches `storage_state`, and that Playwright/network/browser failures in both `verify_storage_state()` and `AuthManager._wait_for_verified_session()`'s direct authoritative `verifier._verify_context()` call return an invalid `AuthVerificationResult` rather than raising. Keep file parsing, malformed JSON, and programming errors outside this conversion boundary.
- [x] Run the focused tests and observe the expected red failures.
- [x] Implement the minimum capture argument forwarding and verifier exception boundary; preserve successful verification fields.
- [x] Run focused auth tests and the full suite.
- [x] Commit with a focused conventional message.

### Task 2: Reclaim helper login capacity

**Files:**
- Modify: `cairn/src/cairn/auth_helper/daemon.py`
- Test: `cairn/tests/test_auth_helper.py`

- [x] Write regressions showing an exited launched process is removed from `_active` before pending requests are admitted, while a running process remains active.
- [x] Run them red, change active-entry bookkeeping to retain the launched process (or equivalent), poll safely before admission, and reclaim only exited or failed-to-launch entries. Define non-auto-launch entries so they cannot permanently consume a process slot.
- [x] Run the full suite and commit with a focused conventional message.

### Task 3: Enforce auth-intervention configuration

**Files:**
- Modify: `cairn/src/cairn/dispatcher/tasks/reason.py`
- Test: `cairn/tests/test_auth_intervention.py` and its existing auth-intervention fixtures

- [x] Update conflicting fixtures that currently expect an AuthRequest with `auth=None`, then write regressions proving that `auth=None`, disabled intervention, and unknown auth targets produce no AuthRequest.
- [x] Run red, add the smallest dispatcher-side gate while keeping normal intents unaffected. For configured/enabled targets, continue taking role from configuration and enforcing `allow_roles`.
- [x] Run the focused test file and full suite, then commit with a focused conventional message.
