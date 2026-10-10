from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


OPENAI_PROVIDER_APIS = frozenset({"openai", "openai-compatible", "openai-completions", "openai-responses"})


def requires_openai_v1_base_url(endpoint_type: str | None, provider_api: str | None = None) -> bool:
    if endpoint_type == "codex":
        return True
    if endpoint_type != "pi":
        return False
    return (provider_api or "").strip().lower() in OPENAI_PROVIDER_APIS


def normalize_openai_v1_base_url(base_url: str) -> str:
    url = base_url.strip().rstrip("/")
    parsed = urlsplit(url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if any(segment == "v1" for segment in segments):
        return url
    path = f"{parsed.path.rstrip('/')}/v1" if parsed.path else "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def normalize_provider_base_url(
    *,
    endpoint_type: str | None,
    base_url: str,
    provider_api: str | None = None,
) -> str:
    if requires_openai_v1_base_url(endpoint_type, provider_api):
        return normalize_openai_v1_base_url(base_url)
    return base_url.strip()
