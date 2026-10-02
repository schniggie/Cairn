from __future__ import annotations

import json
import re
from typing import Any


FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.IGNORECASE | re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    seen: set[str] = set()

    for candidate in _candidate_segments(text):
        segment = candidate.strip()
        if not segment or segment in seen:
            continue
        seen.add(segment)

        try:
            parsed = json.loads(segment)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, dict):
                return parsed

        for start in _object_start_positions(segment):
            try:
                parsed, _end = decoder.raw_decode(segment[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

    raise ValueError("no JSON object found in output")


def parse_manual_conclusion_payload(text: str) -> dict[str, str | None]:
    payload = extract_json_object(text)
    accepted = payload.get("accepted")
    if accepted is False:
        raise ValueError("accepted=false cannot conclude an intent")
    if accepted is True:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("data must be an object")
    elif _looks_like_fact_payload(payload):
        data = payload
    else:
        raise ValueError("accepted must be true or false")
    return parse_fact_payload(data, field="data")


def parse_fact_payload(payload: Any, *, field: str = "fact") -> dict[str, str | None]:
    if not isinstance(payload, dict):
        raise ValueError(f"{field} is required")
    description = payload.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"{field}.description is required")
    title = payload.get("title")
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise ValueError(f"{field}.title must be a non-empty string")
    return {
        "title": title.strip() if isinstance(title, str) else None,
        "description": description.strip(),
    }


def _candidate_segments(text: str) -> list[str]:
    segments = [text.strip()]
    segments.extend(match.group(1).strip() for match in FENCED_BLOCK_RE.finditer(text))
    return segments


def _object_start_positions(text: str) -> list[int]:
    return [index for index, char in enumerate(text) if char == "{"]


def _looks_like_fact_payload(payload: dict[str, Any]) -> bool:
    return set(payload) in ({"description"}, {"title", "description"})
