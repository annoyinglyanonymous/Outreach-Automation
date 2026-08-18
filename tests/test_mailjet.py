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


# ---- list_verified_senders: the campaign edit-page sender dropdown -----

# A Mailjet GET /v3/REST/sender listing: verified ('Active') addresses,
# one pending ('Inactive'), and a wildcard domain sender.
SENDER_LIST_BODY = {
    "Count": 4,
    "Data": [
        {"Email": "sales@renegadeinsurance.com", "Status": "Active"},
        {"Email": "automate@renegadeinsurance.com", "Status": "Active"},
        {"Email": "pending@renegadeinsurance.com", "Status": "Inactive"},
        {"Email": "*@renegadeinsurance.com", "Status": "Active"},
    ],
    "Total": 4,
}


@pytest.mark.asyncio
async def test_list_verified_senders_returns_only_active_individuals_sorted():
    """Verified individual addresses only: pending ('Inactive') senders and
    wildcard domain senders ('*@...', not a usable From) are dropped, and
    the result is sorted so the dropdown order is stable."""
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization", "")
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json=SENDER_LIST_BODY)

    result = await sender_with(handler).list_verified_senders()

    assert result == ["automate@renegadeinsurance.com", "sales@renegadeinsurance.com"]
    assert seen["method"] == "GET"
    assert seen["url"] == "https://api.mailjet.com/v3/REST/sender"
    assert seen["auth"].startswith("Basic ")  # key/secret pair, like send()


@pytest.mark.asyncio
async def test_list_verified_senders_non_200_raises_provider_error():
    def handler(request):
        return httpx.Response(401, json={"ErrorMessage": "bad key"})

    with pytest.raises(ProviderError, match="401"):
        await sender_with(handler).list_verified_senders()


@pytest.mark.asyncio
async def test_list_verified_senders_transport_failure_raises_provider_error():
    """A dead Mailjet is a ProviderError, never an unhandled exception —
    the route catches it and degrades the dropdown to manual entry."""
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ProviderError, match="unreachable"):
        await sender_with(handler).list_verified_senders()


@pytest.mark.asyncio
async def test_list_verified_senders_without_keys_makes_no_request():
    """Unset keys short-circuit before any HTTP — this is what keeps the
    edit page from hitting the network in tests (and unconfigured installs)."""
    def handler(request):  # must never be called
        raise AssertionError("no request expected without keys")

    sender = MailjetSender(api_key="", secret_key="",
                           transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="not configured"):
        await sender.list_verified_senders()


# ---- list_verified_sender_records: names for the auto-enrol pool sync ----

SENDER_RECORDS_BODY = {
    "Count": 4,
    "Data": [
        {"Email": "sales@renegadeinsurance.com", "Name": "Renegade Sales", "Status": "Active"},
        {"Email": "automate@renegadeinsurance.com", "Name": "", "Status": "Active"},
        {"Email": "pending@renegadeinsurance.com", "Name": "Later", "Status": "Inactive"},
        {"Email": "*@renegadeinsurance.com", "Name": "Domain", "Status": "Active"},
    ],
    "Total": 4,
}


@pytest.mark.asyncio
async def test_list_verified_sender_records_carries_names_for_auto_enrol():
    """The pool auto-enrol sync needs Mailjet's display Name, not just the
    address, so the From it enrols carries the name Mailjet already knows.
    Same Active-only / no-wildcard filter and sort as the email projection;
    a blank Name collapses to None (no display name rather than '')."""
    def handler(request):
        return httpx.Response(200, json=SENDER_RECORDS_BODY)

    records = await sender_with(handler).list_verified_sender_records()

    assert records == [
        {"email": "automate@renegadeinsurance.com", "name": None},
        {"email": "sales@renegadeinsurance.com", "name": "Renegade Sales"},
    ]


@pytest.mark.asyncio
async def test_list_verified_senders_is_a_projection_of_the_records():
    """The email-only helper (the datalist) is exactly the records' emails,
    so the two can never drift."""
    def handler(request):
        return httpx.Response(200, json=SENDER_RECORDS_BODY)

    sender = sender_with(handler)
    records = await sender.list_verified_sender_records()

    def handler2(request):
        return httpx.Response(200, json=SENDER_RECORDS_BODY)

    emails = await sender_with(handler2).list_verified_senders()
    assert emails == [r["email"] for r in records]
