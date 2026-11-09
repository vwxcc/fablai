from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .providers import Provider, get_provider_pool, provider_semaphore, record_service_call


class ProviderUnavailable(RuntimeError):
    pass


def _chat_url(base_url: str) -> str:
    value = base_url.rstrip("/")
    if value.endswith("/chat/completions"):
        return value
    if value.endswith("/v1"):
        return value + "/chat/completions"
    return value + "/v1/chat/completions"


def _headers(provider: Provider) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = "Bearer " + provider.api_key
    return headers


async def chat_completion(
    task_type: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    forced_model: str | None = None,
) -> tuple[dict[str, Any], Provider]:
    pool = await get_provider_pool(task_type)
    if forced_model:
        pool = [p for p in pool if p.model == forced_model]
    if not pool:
        raise ProviderUnavailable("No enabled provider connection for task: " + task_type)

    errors: list[str] = []
    for provider in pool:
        if not provider.api_key:
            errors.append(provider.label + ": API key is missing")
            continue
        payload: dict[str, Any] = {
            "model": provider.model,
            "messages": messages,
            "temperature": max(0.0, min(2.0, float(temperature))),
            "max_tokens": max(1, min(32768, int(max_tokens))),
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        sem = await provider_semaphore(provider)
        try:
            async with sem:
                async with httpx.AsyncClient(timeout=provider.timeout_seconds) as client:
                    response = await client.post(
                        _chat_url(provider.base_url),
                        headers=_headers(provider),
                        json=payload,
                    )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
                    raise ProviderUnavailable("Provider response has no choices")
                await record_service_call(provider.id, True)
                return data, provider
        except (httpx.HTTPError, ValueError, ProviderUnavailable) as exc:
            await record_service_call(provider.id, False)
            errors.append(provider.label + ": " + str(exc)[:240])
            continue
    raise ProviderUnavailable("All " + task_type + " providers failed: " + "; ".join(errors))


async def audio_request(
    task_type: str,
    endpoint: str,
    *,
    data: dict[str, Any] | None = None,
    file_field: tuple[str, bytes, str] | None = None,
) -> tuple[bytes, str, Provider]:
    pool = await get_provider_pool(task_type)
    if not pool:
        raise ProviderUnavailable("No enabled provider connection for task: " + task_type)
    errors: list[str] = []
    for provider in pool:
        headers = {}
        if provider.api_key:
            headers["Authorization"] = "Bearer " + provider.api_key
        base = provider.base_url.rstrip("/")
        if base.endswith("/v1"):
            url = base + endpoint
        else:
            url = base + "/v1" + endpoint
        sem = await provider_semaphore(provider)
        try:
            async with sem:
                async with httpx.AsyncClient(timeout=provider.timeout_seconds) as client:
                    if file_field:
                        name, content, mime = file_field
                        form_data = dict(data or {})
                        if endpoint == "/audio/transcriptions":
                            form_data["model"] = provider.model
                        response = await client.post(
                            url, headers=headers,
                            data=form_data,
                            files={"file": (name, content, mime)},
                        )
                    else:
                        response = await client.post(url, headers=headers, json=data or {})
                response.raise_for_status()
                await record_service_call(provider.id, True)
                return response.content, response.headers.get("content-type", "application/octet-stream"), provider
        except httpx.HTTPError as exc:
            await record_service_call(provider.id, False)
            errors.append(provider.label + ": " + str(exc)[:200])
    raise ProviderUnavailable("All " + task_type + " providers failed: " + "; ".join(errors))
