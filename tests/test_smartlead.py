"""Smartlead sender transport tests — httpx.MockTransport, no network."""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import config
from app.providers.base import ProviderError, SendRejected
from app.providers.smartlead import SmartleadSender
from app.repo import EmailTarget

TARGET = EmailTarget(
    id=7,
    email="jane@doe.example",
    first_name="Jane",
    last_name="Doe",
    company="Doe Insurance",
    email_subject="Quick question",
    email_body="Body text",
    consent_status="cold",
    smartlead_campaign_id="sl-123",
    sender_email=None,
    sender_name="Rojan",
)


def sender_with(handler) -> SmartleadSender:
    return SmartleadSender(api_key="sl_test", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_lead_payload_carries_personalised_copy():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "upload_count": 1})

    ref = await sender_with(handler).send(TARGET)

    assert ref == "lead-added"
    assert "/campaigns/sl-123/leads" in seen["url"]
    assert "api_key=sl_test" in seen["url"]
    lead = seen["body"]["lead_list"][0]
    assert lead["email"] == "jane@doe.example"
    assert lead["custom_fields"] == {
        "personalized_subject": "Quick question",
        "personalized_body": "Body text",
    }
    # Smartlead's own block/unsubscribe lists stay active — it is the
    # compliance layer for cold sends.
    assert seen["body"]["settings"]["ignore_unsubscribe_list"] is False


@pytest.mark.asyncio
async def test_duplicate_lead_is_success():
    """Idempotent replay: a lead already in the campaign must read as
    sent, never as a failure — this is what makes crash-recovery and
    retry release safe."""
    def handler(request):
        return httpx.Response(200, json={
            "ok": True, "upload_count": 0, "already_added_to_campaign": 1,
        })

    assert await sender_with(handler).send(TARGET) == "duplicate-skip"


@pytest.mark.asyncio
async def test_invalid_email_is_a_per_contact_rejection():
    def handler(request):
        return httpx.Response(200, json={
            "ok": True, "upload_count": 0, "invalid_email_count": 1,
        })

    with pytest.raises(SendRejected):
        await sender_with(handler).send(TARGET)


@pytest.mark.asyncio
async def test_unrecognised_response_is_a_provider_error():
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    with pytest.raises(ProviderError, match="unrecognised"):
        await sender_with(handler).send(TARGET)


@pytest.mark.asyncio
async def test_401_fails_immediately():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(401, json={"message": "bad key"})

    with pytest.raises(ProviderError, match="401"):
        await sender_with(handler).send(TARGET)
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_5xx_retries_then_gives_up(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_MAX_RETRIES", 2)
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(502, headers={"Retry-After": "0"}, json={})

    with pytest.raises(ProviderError, match="gave up"):
        await sender_with(handler).send(TARGET)
    assert attempts["n"] == 2
