"""Local fake CTF platform used for development and integration tests.

Simulates a CTFd-like platform with three challenges:

- ``mock-hello``  — a "correct flag" challenge (``flag{hello}``).
- ``mock-trap``   — always rejects every flag (exercises the reopen loop).
- ``mock-flood``  — rate-limits the first wrong attempt, then rejects
  (exercises rate-limit backoff). The correct flag ``flag{flooded}`` always
  succeeds immediately.

Submission state lives in memory, so each process starts clean.
"""

from __future__ import annotations

from cairn.ctfbridge.adapters.base import Challenge, ChallengeSource, SubmissionResult

_MOCK_DEFAULT = [
    Challenge(
        external_id="mock-hello",
        title="Hello Flag",
        category="welcome",
        points=10,
        description="A friendly welcome challenge. The flag format is flag{...}.",
        target="10.0.0.1:5000",
        attachments=["http://mock.example/files/hello.txt"],
        hints=["Try reading the welcome message."],
    ),
    Challenge(
        external_id="mock-trap",
        title="Trap Challenge",
        category="trap",
        points=50,
        description="This challenge never accepts a submitted flag.",
        target="",
        attachments=[],
        hints=[],
    ),
    Challenge(
        external_id="mock-flood",
        title="Flood Challenge",
        category="misc",
        points=100,
        description="Submission endpoint rate-limits quickly.",
        target="10.0.0.2:7000",
        attachments=[],
        hints=[],
    ),
]


class MockSource(ChallengeSource):
    name = "mock"

    def __init__(
        self,
        base_url: str = "http://mock.invalid",
        token: str = "",
        team_name: str = "",
        timeout: float = 5.0,
        challenges: list[Challenge] | None = None,
        **kwargs,  # tolerate adapter-specific options from ctf_config
    ) -> None:
        self.base_url = base_url
        self.token = token
        self.team_name = team_name
        self._challenges = {c.external_id: c for c in (challenges or _MOCK_DEFAULT)}
        self._rate_limit_budget: dict[str, int] = {"mock-flood": 1}
        self._submitted: list[tuple[str, str]] = []
        self._recovered: list[str] = []

    def recover_env(self, external_id: str) -> None:
        self._recovered.append(external_id)

    def verify_connection(self) -> bool:
        if not self.base_url:
            raise RuntimeError("base_url is not configured")
        return True

    def list_challenges(self) -> list[Challenge]:
        return [self._challenges[c] for c in self._challenges]

    def get_challenge(self, external_id: str) -> Challenge:
        try:
            return self._challenges[external_id]
        except KeyError:
            raise RuntimeError(f"challenge {external_id!r} not found") from None

    def submit_flag(self, external_id: str, flag: str) -> tuple[SubmissionResult, str]:
        self._submitted.append((external_id, flag))

        if external_id == "mock-hello":
            return (
                (SubmissionResult.SUCCESS, "flag accepted")
                if flag == "flag{hello}"
                else (SubmissionResult.WRONG_FLAG, "flag rejected")
            )
        if external_id == "mock-trap":
            return SubmissionResult.WRONG_FLAG, "flag rejected"
        if external_id == "mock-flood":
            if flag == "flag{flooded}":
                return SubmissionResult.SUCCESS, "flag accepted"
            budget = self._rate_limit_budget.get(external_id, 0)
            if budget > 0:
                self._rate_limit_budget[external_id] = budget - 1
                return SubmissionResult.RATE_LIMITED, "rate limited, try later"
            return SubmissionResult.WRONG_FLAG, "flag rejected"
        return SubmissionResult.ERROR, f"unknown challenge {external_id}"
