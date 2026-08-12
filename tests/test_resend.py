"""Resend sender transport tests — httpx.MockTransport, no network."""
from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from app.config import config
from app.providers.base import ProviderError, SendRejected
from app.providers.resend import ResendSender
from app.repo import EmailTarget

TARGET = EmailTarget(
    id=7,
    email="jane@doe.example",
    first_name="Jane",
    last_name="Doe",
    company="Doe Insurance",
    email_subject="Quick question",
    email_body="Body text",
    consent_status="opted_in",
    smartlead_campaign_id=None,
    sender_email="rojan@renegadeinsurance.com",
    sender_name="Rojan",
)


def sender_with(handler) -> ResendSender:
    return ResendSender(api_key="re_test", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_send_carries_idempotency_key_and_formatted_from():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["idempotency"] = request.headers["idempotency-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "email_123"})

    ref = await sender_with(handler).send(TARGET)

    assert ref == "email_123"
    assert seen["idempotency"] == "outreach/contact-7"
    assert seen["body"]["from"] == "Rojan <rojan@renegadeinsurance.com>"
    assert seen["body"]["to"] == ["jane@doe.example"]
    assert seen["body"]["subject"] == "Quick question"


@pytest.mark.asyncio
async def test_bare_from_when_no_sender_name():
    seen = {}

    def handler(request):
        seen["from"] = json.loads(request.content)["from"]
        return httpx.Response(200, json={"id": "email_1"})

    target = dataclasses.replace(TARGET, sender_name=None)
    await sender_with(handler).send(target)
    assert seen["from"] == "rojan@renegadeinsurance.com"


@pytest.mark.asyncio
async def test_missing_sender_email_is_rejected_before_any_request():
    def handler(request):  # must never be called
        raise AssertionError("no request expected")

    target = EmailTarget(
        id=1, email="a@b.c", first_name="A", last_name=None, company=None,
        email_subject="S", email_body="B", consent_status="opted_in",
        smartlead_campaign_id=None, sender_email=None, sender_name=None,
    )
    with pytest.raises(SendRejected):
        await sender_with(handler).send(target)


@pytest.mark.asyncio
async def test_409_idempotency_conflict_treated_as_already_sent():
    def handler(request):
        return httpx.Response(409, json={"name": "invalid_idempotent_request"})

    assert await sender_with(handler).send(TARGET) == "idempotent-conflict"


@pytest.mark.asyncio
async def test_422_is_a_per_contact_rejection():
    def handler(request):
        return httpx.Response(422, json={"message": "invalid to address"})

    with pytest.raises(SendRejected):
        await sender_with(handler).send(TARGET)


@pytest.mark.asyncio
async def test_401_is_a_provider_error_without_retry():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(401, json={"message": "bad key"})

    with pytest.raises(ProviderError, match="401"):
        await sender_with(handler).send(TARGET)
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_429_retries_then_succeeds():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={"id": "email_after_retry"})

    assert await sender_with(handler).send(TARGET) == "email_after_retry"
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_MAX_RETRIES", 2)

    def handler(request):
        return httpx.Response(503, headers={"Retry-After": "0"}, json={})

    with pytest.raises(ProviderError, match="gave up"):
        await sender_with(handler).send(TARGET)
