"""Email runner tests — fakes only. These lock in the money-grade
semantics: per-send immediate writes, no stale-claim reset, consent
routing, and vendor-failure release that keeps already-sent work."""
from __future__ import annotations

import pytest

from app import emailer, repo
from app.config import config
from app.providers.base import ProviderError, SendRejected, SendUncertain
from app.repo import EmailTarget


def target(id=1, consent="cold", **overrides) -> EmailTarget:
    fields = {
        "id": id,
        "email": f"agent{id}@example.com",
        "first_name": "Jane",
        "last_name": "Doe",
        "company": "Doe Insurance",
        "email_subject": "S",
        "email_body": "B",
        "consent_status": consent,
        # Both paths now send from a verified sender_email (cold -> Mailjet,
        # opted_in -> Resend); smartlead_campaign_id is inert.
        "smartlead_campaign_id": None,
        "sender_email": "r@x.com",
        "sender_name": "Rojan",
    }
    fields.update(overrides)
    return EmailTarget(**fields)


class FakeSender:
    def __init__(self, name, ops, queue=None):
        self.name = name
        self._ops = ops
        self.queue = list(queue or [])

    async def send(self, t):
        self._ops.append(f"send:{self.name}:{t.id}")
        item = self.queue.pop(0) if self.queue else "ref"
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def state(monkeypatch):
    s = {
        "claims": [],          # queue of batches for claim_email_batch
        "claim_consents": [],  # what consents each claim was called with
        "ops": [],             # interleaved send/mark operations
        "failed": [],
        "released": [],
        "stuck": 0,
        "swept": 0,
    }

    async def claim_email_batch(consents, limit=None):
        s["claim_consents"].append(list(consents))
        return s["claims"].pop(0) if s["claims"] else []

    async def mark_email_sent(cid, provider, ref):
        s["ops"].append(f"mark:{cid}:{ref}")
        return True

    async def mark_email_failed(cid, provider, reason):
        s["failed"].append((cid, provider, reason))
        return True

    async def release_email_claims(ids):
        s["released"].append(list(ids))
        return len(ids)

    async def count_stuck_sending():
        return s["stuck"]

    async def sweep_suppressed():
        s["ops"].append("sweep")
        return s["swept"]

    for fn in (claim_email_batch, mark_email_sent, mark_email_failed,
               release_email_claims, count_stuck_sending, sweep_suppressed):
        monkeypatch.setattr(repo, fn.__name__, fn)

    return s


@pytest.mark.asyncio
async def test_marks_each_send_immediately_not_batched(state):
    """The write follows every vendor accept before the next send starts —
    a crash can never lose more than the one in-flight contact."""
    senders = {"cold": FakeSender("mailjet", state["ops"], ["r1", "r2"])}
    state["claims"] = [[target(1), target(2)]]

    stats = await emailer.run(senders)

    assert state["ops"] == ["sweep",
                            "send:mailjet:1", "mark:1:r1",
                            "send:mailjet:2", "mark:2:r2"]
    assert stats.sent == 2


@pytest.mark.asyncio
async def test_routes_by_consent(state):
    ops = state["ops"]
    senders = {
        "cold": FakeSender("mailjet", ops),
        "opted_in": FakeSender("resend", ops),
    }
    state["claims"] = [[target(1, "cold"), target(2, "opted_in")]]

    await emailer.run(senders)

    assert "send:mailjet:1" in ops
    assert "send:resend:2" in ops


@pytest.mark.asyncio
async def test_claim_only_sees_configured_consents(state):
    senders = {"cold": FakeSender("mailjet", state["ops"])}
    await emailer.run(senders)
    assert state["claim_consents"] == [["cold"]]


@pytest.mark.asyncio
async def test_sweep_runs_before_any_claim(state):
    senders = {"cold": FakeSender("mailjet", state["ops"])}
    state["claims"] = [[target(1)]]
    state["swept"] = 3

    stats = await emailer.run(senders)

    assert state["ops"][0] == "sweep"
    assert stats.suppressed == 3


@pytest.mark.asyncio
async def test_rejection_marks_failed_and_continues(state):
    senders = {"cold": FakeSender("mailjet", state["ops"],
                                  [SendRejected("bad address"), "r2"])}
    state["claims"] = [[target(1), target(2)]]

    stats = await emailer.run(senders)

    assert state["failed"] == [(1, "mailjet", "bad address")]
    assert "mark:2:r2" in state["ops"]
    assert stats.rejected == 1
    assert stats.sent == 1


@pytest.mark.asyncio
async def test_vendor_down_releases_current_and_rest_keeps_prior_sends(state):
    senders = {"cold": FakeSender("mailjet", state["ops"],
                                  ["r1", ProviderError("smartlead: gave up")])}
    state["claims"] = [[target(1), target(2), target(3)]]

    stats = await emailer.run(senders)

    assert "mark:1:r1" in state["ops"]           # already-sent work kept
    assert state["released"] == [[2, 3]]         # current + unprocessed released
    assert stats.sent == 1
    assert stats.released == 2
    assert stats.errors


@pytest.mark.asyncio
async def test_uncertain_send_is_left_in_sending_not_released(state):
    """Mailjet has no idempotency key, so an ambiguous send must NOT be
    released (a replay could double-send) nor marked failed — the contact
    stays 'sending' to be surfaced as stuck. The untried remainder, which
    never left, IS released, and the run stops."""
    senders = {"cold": FakeSender("mailjet", state["ops"],
                                  ["r1", SendUncertain("mailjet: ambiguous")])}
    state["claims"] = [[target(1), target(2), target(3)]]

    stats = await emailer.run(senders)

    assert "mark:1:r1" in state["ops"]   # the committed send stands
    assert state["failed"] == []         # the uncertain contact is NOT failed
    assert state["released"] == [[3]]    # only the untried remainder released;
                                         # contact 2 is left at 'sending'
    assert stats.uncertain == 1
    assert stats.sent == 1
    assert stats.errors


@pytest.mark.asyncio
async def test_unknown_consent_released_not_sent(state):
    senders = {"cold": FakeSender("mailjet", state["ops"])}
    state["claims"] = [[target(1, consent="unknown"), target(2, "cold")]]

    stats = await emailer.run(senders)

    assert state["released"] == [[1]]
    assert "send:mailjet:2" in state["ops"]
    assert stats.errors


@pytest.mark.asyncio
async def test_stuck_sending_is_surfaced_never_reset(state):
    state["stuck"] = 4
    stats = await emailer.run({"cold": FakeSender("mailjet", state["ops"])})
    assert stats.stuck_sending == 4
    assert state["released"] == []  # nothing touched


@pytest.mark.asyncio
async def test_no_senders_configured_claims_nothing(state):
    stats = await emailer.run({})
    assert state["claim_consents"] == [[]]
    assert stats.claimed == 0


@pytest.mark.asyncio
async def test_multi_pass_drain_and_cap(state, monkeypatch):
    monkeypatch.setattr(config, "SEND_BATCH_SIZE", 1)
    senders = {"cold": FakeSender("mailjet", state["ops"])}
    state["claims"] = [[target(i)] for i in range(1, 6)]

    stats = await emailer.run(senders, max_passes=3)

    assert stats.passes == 3
    assert stats.sent == 3


def test_build_senders_uses_mailjet_for_cold_when_configured(monkeypatch):
    from app.providers.mailjet import MailjetSender
    monkeypatch.setattr(config, "MAILJET_API_KEY", "k")
    monkeypatch.setattr(config, "MAILJET_SECRET_KEY", "s")
    assert isinstance(emailer.build_senders()["cold"], MailjetSender)


def test_build_senders_falls_back_to_smartlead_for_cold(monkeypatch):
    """Only during cutover: Smartlead serves cold if the Mailjet pair
    (unset by default here) isn't configured."""
    from app.providers.smartlead import SmartleadSender
    monkeypatch.setattr(config, "SMARTLEAD_API_KEY", "sl")
    assert isinstance(emailer.build_senders()["cold"], SmartleadSender)
