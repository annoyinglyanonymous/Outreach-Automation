"""n8n LLM provider tests — MockTransport only. The webhook fronts the
org's OpenAI credential; contract is {system, user} in, the model's JSON
object out."""
from __future__ import annotations

import json

import httpx
import pytest

from app import campaign_brief, drafting
from app.config import config
from app.providers.base import ProviderError
from app.providers.n8n_llm import N8nDrafter

URL = "https://n8n.example/webhook/llm-json"

DRAFT_JSON = {"subject": "Quick question", "body": "Hi Jane — worth a call?",
              "linkedin_note": "Enjoyed your post on wind renewals."}


def sender(payload, status=200, capture=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(json.loads(request.content))
        body = payload if isinstance(payload, str) else json.dumps(payload)
        return httpx.Response(status, content=body,
                              headers={"content-type": "application/json"})

    return N8nDrafter(url=URL, transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_draft_happy_path_sends_contract_payload():
    seen: list[dict] = []
    draft = await sender(DRAFT_JSON, capture=seen).draft("SYSTEM", "USER")

    assert draft.subject == "Quick question"
    assert draft.linkedin_note.startswith("Enjoyed")
    assert seen[0]["user"] == "USER"
    assert seen[0]["system"].startswith("SYSTEM")
    assert "JSON" in seen[0]["system"]  # shape instruction appended


@pytest.mark.asyncio
async def test_empty_draft_is_provider_error():
    with pytest.raises(ProviderError):
        await sender({"subject": "", "body": ""}).draft("S", "U")


@pytest.mark.asyncio
async def test_workflow_error_shape_is_provider_error():
    with pytest.raises(ProviderError, match="model returned junk"):
        await sender({"error": "model returned junk"}).complete_json("S", "U")


@pytest.mark.asyncio
async def test_non_object_response_is_provider_error():
    with pytest.raises(ProviderError):
        await sender([1, 2, 3]).complete_json("S", "U")


@pytest.mark.asyncio
async def test_404_names_the_inactive_workflow_trap():
    with pytest.raises(ProviderError, match="404"):
        await sender("not registered", status=404).complete_json("S", "U")


@pytest.mark.asyncio
async def test_5xx_retried_twice_then_gives_up(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr("app.providers.n8n_llm.asyncio.sleep", instant)
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(502, text="down")

    drafter = N8nDrafter(url=URL, transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="2 attempts"):
        await drafter.complete_json("S", "U")
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_missing_url_is_provider_error(monkeypatch):
    # Pin the class attribute: the constructor falls back to config, and
    # the real .env carries a live URL.
    monkeypatch.setattr(type(config), "N8N_LLM_URL", "")
    with pytest.raises(ProviderError, match="not configured"):
        await N8nDrafter().complete_json("S", "U")


def test_missing_draft_vars_for_n8n(monkeypatch):
    monkeypatch.setattr(type(config), "DRAFT_PROVIDER", "n8n")
    monkeypatch.setattr(type(config), "N8N_LLM_URL", "")
    assert config.missing_draft_vars() == ["N8N_LLM_URL"]
    monkeypatch.setattr(type(config), "N8N_LLM_URL", URL)
    assert config.missing_draft_vars() == []


def test_build_drafter_selects_n8n(monkeypatch):
    monkeypatch.setattr(type(config), "DRAFT_PROVIDER", "n8n")
    monkeypatch.setattr(type(config), "N8N_LLM_URL", URL)
    assert isinstance(drafting.build_drafter(), N8nDrafter)


@pytest.mark.asyncio
async def test_brief_expansion_routes_through_n8n(monkeypatch):
    brief = {
        "offer_description": "Platform", "cta": "Call?", "tone": "direct",
        "audience_rationale": "Agencies",
        "fallback_email_subject": "Hi {{company}}",
        "fallback_email_body": "Hi {{first_name}} {{sender}}",
    }
    monkeypatch.setattr(type(config), "DRAFT_PROVIDER", "n8n")
    monkeypatch.setattr(type(config), "N8N_LLM_URL", URL)

    def handler(request):
        return httpx.Response(200, json=brief)

    # Patch the class so expand_objective's internally-built expander
    # uses the mock transport.
    import app.providers.n8n_llm as mod
    original_init = mod.N8nDrafter.__init__

    def patched_init(self, url=None, transport=None):
        original_init(self, url=url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(mod.N8nDrafter, "__init__", patched_init)

    fields, source = await campaign_brief.expand_objective("Obj", "Dana", None)
    assert source == "llm"
    assert fields["offer_description"] == "Platform"


@pytest.mark.asyncio
async def test_brief_falls_back_when_n8n_url_missing(monkeypatch):
    monkeypatch.setattr(type(config), "DRAFT_PROVIDER", "n8n")
    monkeypatch.setattr(type(config), "N8N_LLM_URL", "")
    fields, source = await campaign_brief.expand_objective("Obj", None, None)
    assert source == "fallback"
