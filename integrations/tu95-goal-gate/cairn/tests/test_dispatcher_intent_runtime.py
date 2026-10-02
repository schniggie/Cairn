from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import explore
from cairn.server.models import MAX_CHECKPOINT_BYTES

from conftest import (
    FakeClient,
    FakeContainerManager,
    FakeDriver,
    FakeLease,
    make_config,
    make_intent,
    make_project,
    make_run_claim,
)


def _run(monkeypatch, results: list[ProcessResult], *, claim=None, driver=None, lease=None):
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    containers = FakeContainerManager()
    worker_driver = driver or FakeDriver()
    task_lease = lease or FakeLease()
    pending: Iterator[ProcessResult] = iter(results)
    monkeypatch.setattr(explore, "get_driver", lambda _worker_type: worker_driver)
    monkeypatch.setattr(
        explore.HeartbeatLease,
        "for_intent_run",
        lambda *_args, **_kwargs: task_lease,
    )
    monkeypatch.setattr(explore, "_run_process", lambda *_args, **_kwargs: next(pending))

    outcome = explore.run_explore_task(
        config,
        client,
        containers,
        project,
        "facts:\n- id: f001\n",
        intent,
        config.workers[0],
        claim or make_run_claim(),
        TaskCancellation(),
    )
    return outcome, client, containers, worker_driver, task_lease


def _continue(cursor: int = 1) -> str:
    return (
        '{"accepted":true,"data":{"continue":{"progress":"checked",'
        '"next":"continue","artifacts":[],"cursors":{"items":'
        f"{cursor}"
        '},"milestones":[]}}}'
    )


def _fact() -> str:
    return '{"accepted":true,"data":{"fact":{"description":"confirmed"}}}'


def _oversized_continue() -> str:
    return json.dumps(
        {
            "accepted": True,
            "data": {
                "continue": {
                    "progress": "x" * (MAX_CHECKPOINT_BYTES + 1),
                    "next": "continue",
                    "artifacts": [],
                    "cursors": {},
                    "milestones": [],
                }
            },
        },
        separators=(",", ":"),
    )


def test_continue_yields_run_without_creating_fact(monkeypatch) -> None:
    outcome, client, _containers, _driver, _lease = _run(
        monkeypatch, [ProcessResult(0, _continue(), "")]
    )

    assert outcome == "success"
    assert client.concluded_runs == []
    assert len(client.yielded_runs) == 1
    yielded = client.yielded_runs[0]
    assert yielded["session_id"] == "session-001"
    assert yielded["checkpoint"]["progress"] == "checked"
    assert yielded["evidence"]["cursors"] == {"items": 1}
    assert yielded["max_no_progress_slices"] == 2
    assert yielded["retry_after_seconds"] == 15


@pytest.mark.parametrize(
    ("first", "expected_reason"),
    [
        (ProcessResult(137, "", "", timed_out=True), "timeout"),
        (ProcessResult(0, "not json", ""), "parse_failed"),
        (ProcessResult(0, '{"accepted":true,"data":{}}', ""), "invalid_payload"),
        (ProcessResult(7, "", "failed"), "exit_code:7"),
    ],
)
def test_abnormal_slice_uses_same_session_for_checkpoint(
    monkeypatch, first: ProcessResult, expected_reason: str
) -> None:
    outcome, client, containers, driver, _lease = _run(
        monkeypatch,
        [first, ProcessResult(0, _continue(), "")],
    )

    assert outcome == "success"
    assert len(client.yielded_runs) == 1
    assert client.yielded_runs[0]["session_id"] == "session-001"
    assert len(driver.resume_prompts) == 1
    assert expected_reason in driver.resume_prompts[0]
    assert any("/explore_checkpoint-" in path for _, path, _ in containers.writes)


def test_oversized_main_checkpoint_uses_same_session_checkpoint_path(monkeypatch) -> None:
    outcome, client, _containers, driver, _lease = _run(
        monkeypatch,
        [ProcessResult(0, _oversized_continue(), ""), ProcessResult(0, _continue(), "")],
    )

    assert outcome == "success"
    assert len(driver.resume_prompts) == 1
    assert "invalid_payload" in driver.resume_prompts[0]
    assert client.yielded_runs[0]["checkpoint"]["progress"] == "checked"


def test_rejected_fails_without_checkpoint_or_fact(monkeypatch) -> None:
    outcome, client, containers, driver, _lease = _run(
        monkeypatch,
        [ProcessResult(0, '{"accepted":false,"reason":"policy_refusal"}', "")],
    )

    assert outcome == "rejected"
    assert len(client.failed_runs) == 1
    assert client.concluded_runs == []
    assert client.yielded_runs == []
    assert driver.resume_prompts == []
    assert not any("/explore_checkpoint-" in path for _, path, _ in containers.writes)


def test_cancelled_slice_does_not_checkpoint_or_write_run(monkeypatch) -> None:
    outcome, client, _containers, driver, _lease = _run(
        monkeypatch,
        [ProcessResult(137, "", "", cancelled=True, cancel_reason="stopped")],
    )

    assert outcome == "cancelled"
    assert client.failed_runs == []
    assert client.yielded_runs == []
    assert client.concluded_runs == []
    assert driver.resume_prompts == []


def test_lost_run_lease_does_not_checkpoint_or_write(monkeypatch) -> None:
    lease = FakeLease()
    lease.failure = SimpleNamespace(status_code=409)

    outcome, client, _containers, driver, _lease = _run(
        monkeypatch,
        [ProcessResult(0, _continue(), "")],
        lease=lease,
    )

    assert outcome == "failed"
    assert client.failed_runs == []
    assert client.yielded_runs == []
    assert client.concluded_runs == []
    assert driver.resume_prompts == []


def test_failed_checkpoint_preserves_previous_checkpoint_and_counts_no_progress(
    monkeypatch,
) -> None:
    previous = {
        "progress": "old",
        "next": "resume",
        "artifacts": [],
        "cursors": {"items": 1},
        "milestones": [],
    }
    claim = make_run_claim(
        execution_mode="session",
        session_id="persisted-session",
        checkpoint=previous,
    )
    claim.evidence.cursors = {"items": 1}

    outcome, client, _containers, _driver, _lease = _run(
        monkeypatch,
        [ProcessResult(0, "not json", ""), ProcessResult(1, "", "failed")],
        claim=claim,
    )

    assert outcome == "success"
    assert client.yielded_runs[0]["checkpoint"] == previous
    assert client.yielded_runs[0]["evidence"]["cursors"] == {"items": 1}


def test_oversized_checkpoint_output_preserves_previous_checkpoint(monkeypatch) -> None:
    previous = {
        "progress": "old",
        "next": "resume",
        "artifacts": [],
        "cursors": {"items": 1},
        "milestones": [],
    }
    claim = make_run_claim(
        execution_mode="session",
        session_id="persisted-session",
        checkpoint=previous,
    )
    claim.evidence.cursors = {"items": 1}

    outcome, client, _containers, _driver, _lease = _run(
        monkeypatch,
        [ProcessResult(0, "not json", ""), ProcessResult(0, _oversized_continue(), "")],
        claim=claim,
    )

    assert outcome == "success"
    assert client.failed_runs == []
    assert client.yielded_runs[0]["checkpoint"] == previous


def test_oversized_checkpoint_output_without_previous_checkpoint_fails(monkeypatch) -> None:
    outcome, client, _containers, _driver, _lease = _run(
        monkeypatch,
        [ProcessResult(0, "not json", ""), ProcessResult(0, _oversized_continue(), "")],
    )

    assert outcome == "failed"
    assert client.yielded_runs == []
    assert len(client.failed_runs) == 1


def test_checkpoint_prompt_build_failure_preserves_previous_checkpoint(monkeypatch) -> None:
    previous = {
        "progress": "old",
        "next": "resume",
        "artifacts": [],
        "cursors": {"items": 1},
        "milestones": [],
    }
    claim = make_run_claim(
        execution_mode="session",
        session_id="persisted-session",
        checkpoint=previous,
    )
    claim.evidence.cursors = {"items": 1}
    real_snapshot = explore.write_graph_snapshot_reference

    def snapshot_or_fail(*args, phase: str, **kwargs):
        if phase == "explore_checkpoint":
            raise OSError("checkpoint snapshot failed")
        return real_snapshot(*args, phase=phase, **kwargs)

    monkeypatch.setattr(explore, "write_graph_snapshot_reference", snapshot_or_fail)

    outcome, client, _containers, _driver, _lease = _run(
        monkeypatch,
        [ProcessResult(0, "not json", "")],
        claim=claim,
    )

    assert outcome == "success"
    assert client.failed_runs == []
    assert client.yielded_runs[0]["checkpoint"] == previous
    assert client.yielded_runs[0]["evidence"]["cursors"] == {"items": 1}


def test_anomaly_without_session_fails_instead_of_checkpoint(monkeypatch) -> None:
    driver = FakeDriver()
    driver.prepare_session = lambda: None  # type: ignore[method-assign]

    outcome, client, containers, driver, _lease = _run(
        monkeypatch,
        [ProcessResult(0, "not json", "")],
        driver=driver,
    )

    assert outcome == "failed"
    assert len(client.failed_runs) == 1
    assert "no recoverable session" in client.failed_runs[0]["error"]
    assert driver.resume_prompts == []
    assert not any("/explore_checkpoint-" in path for _, path, _ in containers.writes)


def test_session_mode_resumes_existing_session(monkeypatch) -> None:
    claim = make_run_claim(execution_mode="session", session_id="persisted-session")

    outcome, client, _containers, driver, _lease = _run(
        monkeypatch,
        [ProcessResult(0, _fact(), "")],
        claim=claim,
    )

    assert outcome == "success"
    assert driver.execute_prompts == []
    assert len(driver.resume_prompts) == 1
    assert client.concluded_runs[0][4] == "persisted-session"


def test_checkpoint_only_starts_new_session_with_resume_prompt(monkeypatch) -> None:
    claim = make_run_claim(
        execution_mode="checkpoint_only",
        checkpoint={
            "progress": "old",
            "next": "resume",
            "artifacts": [],
            "cursors": {},
            "milestones": [],
        },
        attempt_count=2,
    )

    outcome, client, _containers, driver, _lease = _run(
        monkeypatch,
        [ProcessResult(0, _fact(), "")],
        claim=claim,
    )

    assert outcome == "success"
    assert len(driver.execute_prompts) == 1
    assert "old" in driver.execute_prompts[0]
    assert driver.resume_prompts == []
    assert client.concluded_runs[0][4] == "session-001"
