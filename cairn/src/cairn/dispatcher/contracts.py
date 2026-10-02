from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cairn.dispatcher.output_parser import extract_json_object


@dataclass(slots=True)
class ReasonResult:
    complete: dict[str, Any] | None = None
    intents: list[dict[str, Any]] | None = None
    interventions: list[dict[str, Any]] | None = None
    rejected: bool = False

    def __post_init__(self) -> None:
        if self.intents is None:
            self.intents = []
        if self.interventions is None:
            self.interventions = []

    @property
    def is_noop(self) -> bool:
        return (
            not self.rejected
            and self.complete is None
            and not self.intents
            and not self.interventions
        )


def validate_intervention(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("intervention must be an object")
    if item.get("type") != "auth":
        raise ValueError(f"unsupported intervention type: {item.get('type')!r}")
    required = {"from", "target", "role", "reason"}
    missing = required - set(item)
    if missing:
        raise ValueError(f"missing intervention fields: {sorted(missing)}")
    if not isinstance(item["from"], list) or not item["from"]:
        raise ValueError("intervention from must be a non-empty array")
    if not isinstance(item["target"], str) or not item["target"].strip():
        raise ValueError("intervention target must be a non-empty string")
    if not isinstance(item["role"], str) or not item["role"].strip():
        raise ValueError("intervention role must be a non-empty string")
    if not isinstance(item["reason"], str) or not item["reason"].strip():
        raise ValueError("intervention reason must be a non-empty string")
    if "login_url" in item and item["login_url"] is not None and not isinstance(item["login_url"], str):
        raise ValueError("intervention login_url must be a string or null")
    return item


def detach_http_records(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Remove and validate optional HTTP evidence without changing legacy result contracts."""
    cleaned = dict(payload)
    data = cleaned.get("data") if isinstance(cleaned.get("data"), dict) else cleaned
    if data is not cleaned:
        data = dict(data)
        cleaned["data"] = data
    records = data.pop("http_records", [])
    if records is None:
        return cleaned, []
    if not isinstance(records, list):
        raise ValueError("http_records must be an array")
    normalized: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"http_records[{index}] must be an object")
        method = record.get("method")
        url = record.get("url")
        significance = record.get("significance")
        if not isinstance(method, str) or not method.strip():
            raise ValueError(f"http_records[{index}].method is required")
        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"http_records[{index}].url is required")
        if not isinstance(significance, str) or not significance.strip():
            raise ValueError(f"http_records[{index}].significance is required")
        request = record.get("request") or {}
        response = record.get("response") or {}
        if not isinstance(request, dict) or not isinstance(response, dict):
            raise ValueError(f"http_records[{index}] request and response must be objects")
        normalized.append(
            {
                "method": method.strip().upper(),
                "url": url.strip(),
                "request": request,
                "response": response,
                "significance": significance.strip(),
            }
        )
    return cleaned, normalized


def parse_json_output(stdout: str) -> dict[str, Any]:
    return extract_json_object(stdout)


def _unwrap_wrapped_payload(payload: dict[str, Any]) -> tuple[bool | None, dict[str, Any] | None]:
    accepted = payload.get("accepted")
    if accepted is False:
        return False, None
    if accepted is True:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("data must be an object")
        return True, data
    return None, None


def _is_dict(value: Any) -> bool:
    return isinstance(value, dict)


def _looks_like_reason_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if keys == {"complete"}:
        complete = payload["complete"]
        return isinstance(complete, dict) and "from" in complete and "description" in complete
    if keys == {"intents"}:
        return isinstance(payload["intents"], list)
    if keys == {"interventions"}:
        return isinstance(payload["interventions"], list)
    if keys == {"intents", "interventions"}:
        return isinstance(payload["intents"], list) and isinstance(payload["interventions"], list)
    if keys == {"intent"}:
        intent = payload["intent"]
        return isinstance(intent, dict) and "from" in intent and "description" in intent
    return False


def _looks_like_bootstrap_execute_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or set(payload) != {"fact", "complete"}:
        return False
    return _is_dict(payload.get("fact")) and _is_dict(payload.get("complete"))


def _looks_like_bootstrap_conclude_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if keys not in ({"fact"}, {"fact", "complete"}):
        return False
    return _is_dict(payload.get("fact"))


def _looks_like_explore_data(payload: dict[str, Any]) -> bool:
    return isinstance(payload, dict) and set(payload) == {"description"}


def validate_reason_payload(
    payload: dict[str, Any], open_intents_empty: bool, max_intents: int,
) -> ReasonResult:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return ReasonResult(rejected=True)
    if accepted is None:
        if not _looks_like_reason_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    complete = data.get("complete")
    intents = data.get("intents")
    interventions = data.get("interventions")
    if intents is None:
        singular = data.get("intent")
        if isinstance(singular, dict):
            intents = [singular]
    if complete is not None:
        if intents is not None or interventions is not None:
            raise ValueError("complete cannot coexist with intents or interventions")
        if not isinstance(complete, dict) or "from" not in complete or "description" not in complete:
            raise ValueError("invalid complete payload")
        return ReasonResult(complete=complete)
    validated_interventions: list[dict[str, Any]] = []
    if interventions is not None:
        if not isinstance(interventions, list):
            raise ValueError("interventions must be an array")
        for index, intervention in enumerate(interventions):
            try:
                validated_interventions.append(validate_intervention(intervention))
            except ValueError as exc:
                raise ValueError(f"invalid intervention at index {index}: {exc}") from exc
    validated_intents: list[dict[str, Any]] = []
    if intents is not None:
        if not isinstance(intents, list):
            raise ValueError("intents must be an array")
        for index, intent in enumerate(intents):
            if not isinstance(intent, dict) or "from" not in intent or "description" not in intent:
                raise ValueError(f"invalid intent at index {index}")
        validated_intents = intents[:max_intents]
    if not validated_intents and not validated_interventions:
        if open_intents_empty:
            raise ValueError("intents or interventions is required when open_intents is empty")
        return ReasonResult()
    return ReasonResult(intents=validated_intents, interventions=validated_interventions)


def validate_bootstrap_execute_payload(payload: dict[str, Any]) -> tuple[str, dict[str, str] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_execute_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")

    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")

    result = {"fact_description": fact_description.strip()}
    complete = data.get("complete")
    if complete is None:
        raise ValueError("complete is required")
    if not isinstance(complete, dict):
        raise ValueError("complete must be an object")
    complete_description = complete.get("description")
    if not isinstance(complete_description, str) or not complete_description.strip():
        raise ValueError("complete.description is required")
    result["complete_description"] = complete_description.strip()
    return "complete", result


def validate_bootstrap_conclude_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_conclude_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    extra_keys = set(data) - {"fact", "complete"}
    if extra_keys:
        raise ValueError("unexpected keys in conclude payload")
    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")
    return "fact", fact_description.strip()


def validate_explore_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_explore_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is required")
    return "fact", description.strip()


def validate_verify_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Verify emit: harness_result or observations with verification facts."""
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if isinstance(payload, dict) and (
            "harness_result" in payload or "triggered" in payload or "observations" in payload
        ):
            data = payload
        else:
            raise ValueError("accepted must be true or false")
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")

    if "observations" in data:
        observations = data["observations"]
        if not isinstance(observations, list) or not observations:
            raise ValueError("observations must be a non-empty array")
        result: list[dict[str, Any]] = []
        for index, obs in enumerate(observations):
            if not isinstance(obs, dict):
                raise ValueError(f"observation[{index}] must be an object")
            description = obs.get("description")
            if not isinstance(description, str) or not description.strip():
                raise ValueError(f"observation[{index}].description is required")
            item: dict[str, Any] = {"description": description.strip()}
            for key in ("type", "evidence", "verifies", "confidence", "oracle_draft"):
                if key in obs and obs[key] is not None:
                    item[key] = obs[key]
            if obs.get("locations") is not None:
                item["locations"] = obs["locations"]
            if obs.get("why_failed") is not None:
                item["why_failed"] = obs["why_failed"]
            result.append(item)
        return "observations", {"observations": result}

    harness = data.get("harness_result")
    if harness is None and "triggered" in data:
        harness = {
            "triggered": data["triggered"],
            "evidence": data.get("evidence"),
            "request": data.get("request"),
            "response": data.get("response"),
            "why_failed": data.get("why_failed"),
            "observed_routing": data.get("observed_routing"),
        }
    if not isinstance(harness, dict) or "triggered" not in harness:
        raise ValueError("harness_result with triggered is required")
    if not isinstance(harness["triggered"], bool):
        raise ValueError("harness_result.triggered must be boolean")
    out: dict[str, Any] = {
        "harness_result": {
            "triggered": harness["triggered"],
            "evidence": harness.get("evidence"),
            "request": harness.get("request"),
            "response": harness.get("response"),
            "why_failed": harness.get("why_failed"),
            "observed_routing": harness.get("observed_routing"),
        }
    }
    if data.get("verifies"):
        out["verifies"] = data["verifies"]
    return "harness_result", out
