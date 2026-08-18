"""Mailjet sender transport tests — httpx.MockTransport, no network.

Mirrors test_resend.py, plus the one behaviour that is unique to a
provider with no idempotency key: an ambiguous failure AFTER the request
left us must raise SendUncertain (never retried, never released), while a
failure that proves the request never left is safe to retry.
"""
from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from app.config import config
from app.providers.base import ProviderError, SendRejected, SendUncertain
from app.providers.mailjet import MailjetSender
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
    smartlead_campaign_id=None,
    sender_email="rojan@renegadeinsurance.com",
    sender_name="Rojan",
)

# A Mailjet v3.1 accepted-message response.
OK_BODY = {
    "Messages": [
        {
            "Status": "success",
            "CustomID": "outreach-contact-7",
            "To": [
                {
                    "Email": "jane@doe.example",
                    "MessageUUID": "abc-uuid",
                    "MessageID": 987654321,
                    "MessageHref": "https://api.mailjet.com/v3/message/987654321",
                }
            ],
        }
    ]
}


def sender_with(handler) -> MailjetSender:
    return MailjetSender(
        api_key="mj_key", secret_key="mj_secret",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_send_uses_basic_auth_and_v31_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    ref = await sender_with(handler).send(TARGET)

    assert ref == "987654321"                       # MessageID, stringified
    assert seen["auth"].startswith("Basic ")        # key/secret pair, not Bearer
    assert seen["url"] == "https://api.mailjet.com/v3.1/send"
    msg = seen["body"]["Messages"][0]
    assert msg["From"] == {"Email": "rojan@renegadeinsurance.com", "Name": "Rojan"}
    assert msg["To"] == [{"Email": "jane@doe.example"}]
    assert msg["Subject"] == "Quick question"
    assert msg["TextPart"] == "Body text"
    assert msg["CustomID"] == "outreach-contact-7"  # correlation handle for the webhook


@pytest.mark.asyncio
async def test_from_has_no_name_when_sender_name_missing():
    seen = {}

    def handler(request):
        seen["from"] = json.loads(request.content)["Messages"][0]["From"]
        return httpx.Response(200, json=OK_BODY)

    await sender_with(handler).send(dataclasses.replace(TARGET, sender_name=None))
    assert seen["from"] == {"Email": "rojan@renegadeinsurance.com"}  # no Name key


@pytest.mark.asyncio
async def test_missing_sender_email_is_rejected_before_any_request():
    def handler(request):  # must never be called
        raise AssertionError("no request expected")

    target = dataclasses.replace(TARGET, sender_email=None)
    with pytest.raises(SendRejected):
        await sender_with(handler).send(target)


@pytest.mark.asyncio
async def test_200_with_error_status_is_a_per_contact_rejection():
    def handler(request):
        return httpx.Response(200, json={"Messages": [{"Status": "error",
                              "Errors": [{"ErrorMessage": "invalid recipient"}]}]})

    with pytest.raises(SendRejected):
        await sender_with(handler).send(TARGET)


@pytest.mark.asyncio
async def test_400_is_a_per_contact_rejection():
    def handler(request):
        return httpx.Response(400, json={"ErrorMessage": "bad recipient"})

    with pytest.raises(SendRejected):
        await sender_with(handler).send(TARGET)


@pytest.mark.asyncio
async def test_401_is_a_provider_error_without_retry():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(401, json={"ErrorMessage": "bad key"})

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
        return httpx.Response(200, json=OK_BODY)

    assert await sender_with(handler).send(TARGET) == "987654321"
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_MAX_RETRIES", 2)

    def handler(request):
        return httpx.Response(503, headers={"Retry-After": "0"}, json={})

    with pytest.raises(ProviderError, match="gave up"):
        await sender_with(handler).send(TARGET)


@pytest.mark.asyncio
async def test_read_timeout_is_uncertain_and_never_retried():
    """A read timeout means Mailjet MAY have accepted the send. With no
    idempotency key, retrying could double-send — so it surfaces as
    SendUncertain on the first occurrence, not a retry."""
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    with pytest.raises(SendUncertain):
        await sender_with(handler).send(TARGET)
    assert attempts["n"] == 1  # not retried — a replay could double-send


@pytest.mark.asyncio
async def test_connect_error_is_retried_then_succeeds(no_backoff):
    """A connection that never opened proves the message did not leave, so
    it is safe to retry (unlike a read timeout)."""
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json=OK_BODY)

    assert await sender_with(handler).send(TARGET) == "987654321"
    assert attempts["n"] == 2
