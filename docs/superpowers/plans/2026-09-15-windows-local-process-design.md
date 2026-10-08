# Windows Local Process Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the local worker process runner execute commands, capture output, and terminate process trees correctly on Windows while preserving the existing POSIX behavior.

**Architecture:** Keep `LocalProcess` as the single cross-platform abstraction. Select process-creation and termination semantics by `os.name`: retain POSIX session/process-group signaling, and use Windows process-group creation plus `taskkill /T /F` for reliable descendant cleanup. Resolve executable commands using the supplied environment's PATH, including Windows executable extensions, so test-installed shims work consistently.

**Tech Stack:** Python 3.12+, `subprocess`, pytest.

---

### Task 1: Cross-platform local worker process handling

**Files:**
- Modify: `cairn/src/cairn/dispatcher/runtime/local_process.py`
- Modify: `cairn/tests/test_local_execution.py`

- [x] **Step 1: Write failing Windows-focused tests**

Add tests that inject a Windows platform seam and verify that executable lookup honours the supplied PATH, Windows process creation does not request a POSIX session, and termination uses a tree-safe Windows command without calling `os.killpg`.

- [x] **Step 2: Run the focused test file and verify the new test fails**

Run: `uv run --project cairn --group dev pytest cairn/tests/test_local_execution.py -q`

Expected: FAIL because the current runner invokes POSIX-only APIs or cannot locate the command shim.

- [x] **Step 3: Implement the smallest platform split**

Preserve the current POSIX code path. On Windows, resolve the command executable with the process environment, use Windows creation flags, and terminate the process tree with a Windows-native mechanism. Keep the public `LocalProcess` API and cancellation result semantics unchanged.

- [x] **Step 4: Verify focused and full regression suites**

Run:
`uv run --project cairn --group dev pytest cairn/tests/test_local_execution.py -q`
`uv run --project cairn --group dev pytest -q`

Expected: zero failures.

- [x] **Step 5: Commit**

```bash
git add cairn/src/cairn/dispatcher/runtime/local_process.py cairn/tests/test_local_execution.py
git commit -m "fix: support local workers on Windows"
```
