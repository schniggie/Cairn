from __future__ import annotations

import logging

from cairn.dispatcher.config import AuthTargetConfig
from cairn.dispatcher.protocol.client import CairnClient

LOG = logging.getLogger(__name__)

# The auth broker acts as its own operator: creator == worker == "operator.auth",
# which the server protocol already permits (worker must be null or equal to creator).
AUTH_OPERATOR = "operator.auth"

# Fixed, stable shape of the AuthSessionVerified fact. Never contains secrets.
FACT_VERIFIED = "AuthSessionVerified"
FACT_INVALID = "AuthSessionInvalid"
INVALID_REASON = "authentication check failed"


class AuthGraphAdapter:
    """Maps external auth verification results onto the Cairn Intent -> Fact protocol.

    It never writes Facts directly: it creates an AuthVerify Intent via the normal
    ``create_intent`` protocol and then concludes it, so the server itself produces the
    ``AuthSessionVerified`` / ``AuthSessionInvalid`` Fact.
    """

    def __init__(self, client: CairnClient):
        self.client = client
        self.last_fact_id: str | None = None

    def verified(
        self,
        project_id: str,
        target: AuthTargetConfig,
        *,
        methods: list[str],
        from_ids: list[str] | None = None,
        intent_source_key: str | None = None,
        fact_source_key: str | None = None,
        event_id: str | None = None,
    ) -> str:
        description = self._verified_description(target, methods)
        intent_source_key = intent_source_key or (f"auth-event:{event_id}:intent" if event_id else None)
        fact_source_key = fact_source_key or (f"auth-event:{event_id}:fact" if event_id else None)
        return self._conclude(project_id, description, from_ids=from_ids, intent_source_key=intent_source_key, fact_source_key=fact_source_key)

    def invalid(
        self,
        project_id: str,
        target: AuthTargetConfig,
        *,
        evidence: str,
        from_ids: list[str] | None = None,
        intent_source_key: str | None = None,
        fact_source_key: str | None = None,
        event_id: str | None = None,
    ) -> str:
        description = self._invalid_description(target, evidence)
        intent_source_key = intent_source_key or (f"auth-event:{event_id}:intent" if event_id else None)
        fact_source_key = fact_source_key or (f"auth-event:{event_id}:fact" if event_id else None)
        return self._conclude(project_id, description, from_ids=from_ids, intent_source_key=intent_source_key, fact_source_key=fact_source_key)

    def invalid_ref(
        self,
        project_id: str,
        auth_ref: str,
        *,
        from_ids: list[str] | None = None,
        intent_source_key: str | None = None,
        fact_source_key: str | None = None,
        event_id: str | None = None,
    ) -> str:
        """Record a safe invalid fact when configured target metadata is unavailable."""
        intent_source_key = intent_source_key or (f"auth-event:{event_id}:intent" if event_id else None)
        fact_source_key = fact_source_key or (f"auth-event:{event_id}:fact" if event_id else None)
        description = (
            f"{FACT_INVALID}\n"
            f"target={auth_ref};\n"
            "role=unknown;\n"
            f"evidence={INVALID_REASON}"
        )
        return self._conclude(
            project_id, description, from_ids=from_ids,
            intent_source_key=intent_source_key, fact_source_key=fact_source_key,
        )

    # -- helpers -----------------------------------------------------------
    def _conclude(
        self,
        project_id: str,
        description: str,
        *,
        from_ids: list[str] | None,
        intent_source_key: str | None = None,
        fact_source_key: str | None = None,
    ) -> str:
        sources = from_ids or ["origin"]
        if intent_source_key and hasattr(self.client, "create_auth_graph_intent"):
            response = self.client.create_auth_graph_intent(
                project_id, intent_source_key, sources, "Verify authenticated session",
                creator=AUTH_OPERATOR, worker=AUTH_OPERATOR,
            )
        else:
            response = self.client.create_intent(
                project_id, sources, "Verify authenticated session", AUTH_OPERATOR, worker=AUTH_OPERATOR
            )
        if not response.ok:
            LOG.warning("auth verify intent create failed project=%s status=%s body=%s", project_id, response.status_code, response.text)
            raise RuntimeError(f"failed to create auth verify intent: {response.text}")
        intent = response.data
        if not isinstance(intent, dict) or not intent.get("id"):
            raise RuntimeError("auth verify intent create returned no intent id")
        intent_id = intent["id"]
        if intent_source_key and fact_source_key and hasattr(self.client, "conclude_auth_graph_intent"):
            concluded = self.client.conclude_auth_graph_intent(
                project_id, intent_source_key, fact_source_key, AUTH_OPERATOR, description
            )
        else:
            concluded = self.client.conclude(project_id, intent_id, AUTH_OPERATOR, description)
        if not concluded.ok:
            LOG.warning("auth verify intent conclude failed project=%s intent=%s status=%s body=%s", project_id, intent_id, concluded.status_code, concluded.text)
            raise RuntimeError(f"failed to conclude auth verify intent: {concluded.text}")
        if isinstance(concluded.data, dict):
            fact = concluded.data.get("fact")
            self.last_fact_id = str(concluded.data.get("fact_id") or (fact or {}).get("id")) if (concluded.data.get("fact_id") or isinstance(fact, dict) and fact.get("id")) else None
        return intent_id

    @staticmethod
    def _verified_description(target: AuthTargetConfig, methods: list[str]) -> str:
        method_text = "+".join(methods) if methods else "none"
        return (
            f"{FACT_VERIFIED}\n"
            f"target={target.name};\n"
            f"role={target.role};\n"
            f"scope={target.base_url};\n"
            f"verification={method_text}"
        )

    @staticmethod
    def _invalid_description(target: AuthTargetConfig, evidence: str) -> str:
        LOG.warning("auth verification failed target=%s detail=%s", target.name, evidence)
        return (
            f"{FACT_INVALID}\n"
            f"target={target.name};\n"
            f"role={target.role};\n"
            f"evidence={INVALID_REASON}"
        )
