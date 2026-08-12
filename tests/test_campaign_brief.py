"""Objective-expansion tests — the contract is 'never raises': every
failure degrades to the fallback brief so campaign creation cannot be
blocked by the LLM vendor."""
from __future__ import annotations

import json

import httpx
import pytest

from app import campaign_brief, drafting
from app.config import config
from app.providers.groq import GroqDrafter

GOOD = {
    "offer_description": "Carrier access platform for GA agencies",
    "cta": "15-minute walkthrough this week?",
    "tone": "direct, peer-to-peer",
    "audience_rationale": "Owners of 2-15 producer agencies",
    "fallback_email_subject": "Quick question about {{company}}",
    "fallback_email_body": "Hi {{first_name}} — worth a call?\n\n{{sender}}",
}


def expander_returning(payload: dict | str, status: int = 200) -> GroqDrafter:
    content = payload if isinstance(payload, str) else json.dumps(payload)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={
            "choices": [{"finish_reason": "stop",
                         "message": {"content": content}}],
        })

    return GroqDrafter(api_key="k", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_happy_expansion():
    fields, source = await campaign_brief.expand_objective(
        "Sell the platform", "Dana", "AE", expander=expander_returning(GOOD))
    assert source == "llm"
    assert fields["offer_description"] == GOOD["offer_description"]
    assert set(fields) == set(campaign_brief.EXPANSION_FIELDS)


@pytest.mark.asyncio
async def test_provider_error_falls_back():
    def handler(request):
        return httpx.Response(401, text="bad key")

    expander = GroqDrafter(api_key="k", transport=httpx.MockTransport(handler))
    fields, source = await campaign_brief.expand_objective(
        "Sell the platform", "Dana", None, expander=expander)
    assert source == "fallback"
    assert fields["offer_description"] == "Sell the platform"


@pytest.mark.asyncio
async def test_incomplete_shape_falls_back():
    fields, source = await campaign_brief.expand_objective(
        "Objective", "Dana", None,
        expander=expander_returning({"offer_description": "x"}))
    assert source == "fallback"


@pytest.mark.asyncio
async def test_non_groq_provider_falls_back_without_http(monkeypatch):
    monkeypatch.setattr(type(config), "DRAFT_PROVIDER", "anthropic")
    fields, source = await campaign_brief.expand_objective("Obj", None, None)
    assert source == "fallback"


@pytest.mark.asyncio
async def test_missing_key_falls_back_without_http(monkeypatch):
    monkeypatch.setattr(type(config), "DRAFT_PROVIDER", "groq")
    monkeypatch.setattr(type(config), "GROQ_API_KEY", "")
    fields, source = await campaign_brief.expand_objective("Obj", None, None)
    assert source == "fallback"


def test_generic_fallback_renders_through_drafting():
    """The generic template must use merge fields the send path resolves."""
    fields = campaign_brief.fallback_brief("Objective text")
    rendered = drafting.render_template(fields["fallback_email_body"], {
        "first_name": "Jane", "company": "Doe Insurance", "sender": "Dana",
    })
    assert "Jane" in rendered and "Doe Insurance" in rendered and "Dana" in rendered
    assert "{{" not in rendered
    subject = drafting.render_template(fields["fallback_email_subject"],
                                       {"company": "Doe Insurance"})
    assert "{{" not in subject
