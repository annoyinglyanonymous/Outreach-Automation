"""Objective-expansion tests — the contract is 'never raises': every
failure degrades to the fallback brief so campaign creation cannot be
blocked by the LLM vendor."""
from __future__ import annotations

import httpx
import pytest

from app import campaign_brief, drafting
from app.config import config
from app.providers.n8n_llm import N8nDrafter

GOOD = {
    "offer_description": "Carrier access platform for GA agencies",
    "cta": "15-minute walkthrough this week?",
    "tone": "direct, peer-to-peer",
    "audience_rationale": "Owners of 2-15 producer agencies",
    "fallback_email_subject": "Quick question about {{company}}",
    "fallback_email_body": "Hi {{first_name}} — worth a call?\n\n{{sender}}",
}


def expander_returning(payload: dict, status: int = 200) -> N8nDrafter:
    # The n8n webhook answers with the model's JSON object verbatim.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return N8nDrafter(url="https://n8n.example/webhook/llm",
                      transport=httpx.MockTransport(handler))


def test_expansion_prompt_is_not_industry_hardcoded():
    """The brief must reflect the operator's objective, not assume insurance —
    so a campaign about anything expands faithfully."""
    prompt = campaign_brief.SYSTEM_PROMPT.lower()
    assert "insurance" not in prompt
    assert "this specific objective" in prompt


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
        return httpx.Response(401, text="bad url")

    expander = N8nDrafter(url="https://n8n.example/webhook/llm",
                          transport=httpx.MockTransport(handler))
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
async def test_non_n8n_provider_falls_back_without_http(monkeypatch):
    # anthropic has no complete_json, so brief expansion can't use it.
    monkeypatch.setattr(type(config), "DRAFT_PROVIDER", "anthropic")
    fields, source = await campaign_brief.expand_objective("Obj", None, None)
    assert source == "fallback"


@pytest.mark.asyncio
async def test_missing_url_falls_back_without_http(monkeypatch):
    monkeypatch.setattr(type(config), "DRAFT_PROVIDER", "n8n")
    monkeypatch.setattr(type(config), "N8N_LLM_URL", "")
    fields, source = await campaign_brief.expand_objective("Obj", None, None)
    assert source == "fallback"


def test_generic_fallback_renders_through_drafting():
    """The generic template uses only merge fields the send path resolves, and
    carries NO sign-off — the sending address's signature is appended at send
    time, so {{sender}} no longer appears in the body."""
    fields = campaign_brief.fallback_brief("Objective text")
    rendered = drafting.render_template(fields["fallback_email_body"], {
        "first_name": "Jane", "company": "Doe Insurance", "sender": "Dana",
    })
    assert "Jane" in rendered and "Doe Insurance" in rendered
    assert "Dana" not in rendered   # no signature baked into the body
    assert "{{" not in rendered
    subject = drafting.render_template(fields["fallback_email_subject"],
                                       {"company": "Doe Insurance"})
    assert "{{" not in subject
