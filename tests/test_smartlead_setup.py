"""Smartlead campaign auto-setup tests — MockTransport only. They lock
in the chain order, the exact shell sequence payload, stop-at-first-
failure semantics, and the reduced retry budget."""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import config
from app.providers.base import ProviderError
from app.providers.smartlead import CampaignSetup, SmartleadSender


class Recorder:
    """Routes requests by (method, path suffix); records call order."""

    def __init__(self, overrides=None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.overrides = overrides or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))

        for (method, suffix), response in self.overrides.items():
            if request.method == method and path.endswith(suffix):
                return response() if callable(response) else response

        if path.endswith("/campaigns/create"):
            return httpx.Response(200, json={"ok": True, "id": 4417, "name": body["name"]})
        if path.endswith("/sequences"):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/email-accounts/") and request.method == "GET":
            return httpx.Response(200, json=[{"id": 11, "from_email": "a@x.com"},
                                             {"id": 12, "from_email": "b@x.com"}])
        if path.endswith("/email-accounts"):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/schedule"):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/status"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, text=f"unhandled {path}")


def sender(recorder: Recorder) -> SmartleadSender:
    return SmartleadSender(api_key="k", transport=httpx.MockTransport(recorder.handler))


@pytest.mark.asyncio
async def test_happy_chain_order_and_payloads():
    rec = Recorder()
    setup = await sender(rec).setup_campaign("Georgia agency owners")

    assert setup == CampaignSetup("4417")
    assert [(m, p) for m, p, _ in rec.calls] == [
        ("POST", "/api/v1/campaigns/create"),
        ("POST", "/api/v1/campaigns/4417/sequences"),
        ("GET", "/api/v1/email-accounts/"),
        ("POST", "/api/v1/campaigns/4417/email-accounts"),
        ("POST", "/api/v1/campaigns/4417/schedule"),
        ("POST", "/api/v1/campaigns/4417/status"),
    ]

    create_body = rec.calls[0][2]
    assert create_body == {"name": "Georgia agency owners"}

    seq = rec.calls[1][2]["sequences"][0]
    assert seq["subject"] == "{{personalized_subject}}"
    assert "{{personalized_body}}" in seq["email_body"]
    assert "white-space: pre-wrap" in seq["email_body"]
    assert seq["seq_delay_details"] == {"delay_in_days": 0}

    attach = rec.calls[3][2]
    assert attach == {"email_account_ids": [11, 12]}

    schedule = rec.calls[4][2]
    assert schedule["timezone"] == config.SMARTLEAD_SCHEDULE_TIMEZONE
    assert schedule["days_of_the_week"] == list(config.SMARTLEAD_SCHEDULE_DAYS)
    assert schedule["max_new_leads_per_day"] == config.SMARTLEAD_MAX_NEW_LEADS_PER_DAY

    assert rec.calls[5][2] == {"status": "START"}


@pytest.mark.asyncio
async def test_create_failure_raises_and_stops():
    rec = Recorder({("POST", "/campaigns/create"): httpx.Response(401, text="bad key")})
    with pytest.raises(ProviderError):
        await sender(rec).setup_campaign("X")
    assert len(rec.calls) == 1  # nothing after create


@pytest.mark.asyncio
async def test_schedule_failure_returns_id_and_skips_activate():
    rec = Recorder({("POST", "/schedule"): httpx.Response(400, text="bad tz")})
    setup = await sender(rec).setup_campaign("X")
    assert setup.campaign_id == "4417"
    assert setup.failed_step == "schedule"
    assert not any(p.endswith("/status") for _, p, _ in rec.calls)


@pytest.mark.asyncio
async def test_no_mailboxes_stops_chain():
    rec = Recorder({("GET", "/email-accounts/"): httpx.Response(200, json=[])})
    setup = await sender(rec).setup_campaign("X")
    assert setup.failed_step == "email-accounts"
    # attach/schedule/activate never called
    assert not any(p.endswith("/schedule") or p.endswith("/status")
                   for _, p, _ in rec.calls)


@pytest.mark.asyncio
async def test_mailbox_selection_attaches_only_chosen():
    """A selection attaches only the connected inboxes whose from_email is
    in it (case-insensitive); the rest of the chain is unchanged."""
    rec = Recorder()
    setup = await sender(rec).setup_campaign("X", mailboxes=["A@X.com"])
    assert setup == CampaignSetup("4417")
    attach = next(b for m, p, b in rec.calls
                  if m == "POST" and p.endswith("/email-accounts"))
    assert attach == {"email_account_ids": [11]}   # b@x.com (id 12) excluded


@pytest.mark.asyncio
async def test_empty_selection_attaches_all():
    """None/empty selection keeps the prior behaviour: attach every inbox."""
    rec = Recorder()
    await sender(rec).setup_campaign("X", mailboxes=[])
    attach = next(b for m, p, b in rec.calls
                  if m == "POST" and p.endswith("/email-accounts"))
    assert attach == {"email_account_ids": [11, 12]}


@pytest.mark.asyncio
async def test_selection_matching_nothing_is_a_partial_failure():
    """A selection that resolves to zero live inboxes stops the chain — we
    never silently fall back to blasting from every mailbox."""
    rec = Recorder()
    setup = await sender(rec).setup_campaign("X", mailboxes=["ghost@nowhere.com"])
    assert setup.failed_step == "email-accounts"
    assert not any(p.endswith("/email-accounts") and m == "POST"
                   for m, p, _ in rec.calls)          # attach never posted
    assert not any(p.endswith("/schedule") or p.endswith("/status")
                   for _, p, _ in rec.calls)


@pytest.mark.asyncio
async def test_list_email_accounts_normalises_address():
    rec = Recorder()
    accounts = await sender(rec).list_email_accounts()
    assert accounts == [{"id": 11, "email": "a@x.com"},
                        {"id": 12, "email": "b@x.com"}]


@pytest.mark.asyncio
async def test_setup_retry_budget_is_two_attempts():
    attempts = {"n": 0}

    def create_response():
        attempts["n"] += 1
        return httpx.Response(502, text="down", headers={"Retry-After": "0"})

    rec = Recorder({("POST", "/campaigns/create"): create_response})
    with pytest.raises(ProviderError) as exc:
        await sender(rec).setup_campaign("X")
    assert attempts["n"] == 2
    assert "2 attempts" in str(exc.value)
