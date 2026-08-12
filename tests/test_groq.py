"""Groq drafter transport tests — httpx.MockTransport, no network."""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import config
from app.providers.base import Draft, DraftRefused, ProviderError
from app.providers.groq import GroqDrafter


def ok_response(content: dict | str, finish: str = "stop") -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(200, json={
        "choices": [{"finish_reason": finish, "message": {"content": text}}],
    })


def drafter_with(handler, model="llama-3.3-70b-versatile") -> GroqDrafter:
    return GroqDrafter(api_key="test-key", model=model,
                       transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_parses_a_draft_and_pins_json_mode():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["body"] = body
        seen["auth"] = request.headers["authorization"]
        return ok_response({"subject": "S", "body": "B", "linkedin_note": "N"})

    draft = await drafter_with(handler).draft("system prompt", "user prompt")

    assert draft == Draft("S", "B", "N")
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    # JSON mode requires the word JSON in the messages; the provider
    # appends its own format instruction rather than relying on callers.
    assert "JSON" in seen["body"]["messages"][0]["content"]
    assert "system prompt" in seen["body"]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_empty_note_becomes_none():
    def handler(request):
        return ok_response({"subject": "S", "body": "B", "linkedin_note": "  "})

    draft = await drafter_with(handler).draft("s", "u")
    assert draft.linkedin_note is None


@pytest.mark.asyncio
async def test_reasoning_effort_only_for_gpt_oss():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return ok_response({"subject": "S", "body": "B", "linkedin_note": "N"})

    await drafter_with(handler, model="openai/gpt-oss-120b").draft("s", "u")
    await drafter_with(handler, model="llama-3.3-70b-versatile").draft("s", "u")

    assert bodies[0]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in bodies[1]


@pytest.mark.asyncio
async def test_content_filter_is_a_refusal():
    def handler(request):
        return ok_response("", finish="content_filter")

    with pytest.raises(DraftRefused):
        await drafter_with(handler).draft("s", "u")


@pytest.mark.asyncio
async def test_truncation_is_a_provider_error():
    def handler(request):
        return ok_response({"subject": "S"}, finish="length")

    with pytest.raises(ProviderError, match="truncated"):
        await drafter_with(handler).draft("s", "u")


@pytest.mark.asyncio
async def test_non_json_content_is_a_provider_error():
    def handler(request):
        return ok_response("sorry, I can't do JSON today")

    with pytest.raises(ProviderError, match="not valid JSON"):
        await drafter_with(handler).draft("s", "u")


@pytest.mark.asyncio
async def test_429_retries_then_succeeds():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return ok_response({"subject": "S", "body": "B", "linkedin_note": "N"})

    draft = await drafter_with(handler).draft("s", "u")
    assert draft.subject == "S"
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_401_fails_immediately_without_retry():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    with pytest.raises(ProviderError, match="401"):
        await drafter_with(handler).draft("s", "u")
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_non_json_body_is_a_provider_error():
    """Distinct from a non-JSON *content* field: here the HTTP envelope
    itself is not JSON (a proxy's HTML error page behind a 200). Used to
    escape as json.JSONDecodeError past the runner's ProviderError
    handler and abort the run instead of releasing the contact.
    Fixed 2026-08-12."""
    def handler(request):
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    with pytest.raises(ProviderError, match="non-JSON body"):
        await drafter_with(handler).draft("s", "u")


@pytest.mark.asyncio
async def test_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_MAX_RETRIES", 2)
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(503, headers={"Retry-After": "0"}, json={})

    with pytest.raises(ProviderError, match="gave up"):
        await drafter_with(handler).draft("s", "u")
    assert attempts["n"] == 2
