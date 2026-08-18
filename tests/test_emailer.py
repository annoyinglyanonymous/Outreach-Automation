"""Email runner tests — fakes only. These lock in the money-grade
semantics: per-send immediate writes, no stale-claim reset, From-rotation
through the sender pool, and vendor-failure release that keeps already-sent
work. Mailjet is the only sender."""
from __future__ import annotations

import pytest

from app import emailer, repo
from app.config import config
from app.providers.base import ProviderError, SendRejected, SendUncertain
from app.repo import EmailTarget


def target(id=1, **overrides) -> EmailTarget:
    fields = {
        "id": id,
        "email": f"agent{id}@example.com",
        "first_name": "Jane",
        "last_name": "Doe",
        "company": "Doe Insurance",
        "email_subject": "S",
        "email_body": "B",
        # None from the claim; the rotation pool stamps the From at send time.
        "sender_email": None,
        "sender_name": None,
    }
    fields.update(overrides)
    return EmailTarget(**fields)


class FakeSender:
    name = "mailjet"

    def __init__(self, ops, queue=None):
        self._ops = ops
        self.queue = list(queue or [])

    async def send(self, t):
        self._ops.append(f"send:{t.id}")
        item = self.queue.pop(0) if self.queue else "ref"
        if isinstance(item, Exception):
            raise item
        return item


class CapturingSender:
    """Records the From identity each target arrives with — so a test can
    assert the rotation actually replaced sender_email/sender_name."""
    name = "mailjet"

    def __init__(self):
        self.seen: list = []

    async def send(self, t):
        self.seen.append((t.id, t.sender_email, t.sender_name))
        return "ref"


@pytest.fixture
def state(monkeypatch):
    s = {
        "claims": [],       # queue of batches for claim_email_batch
        "claim_calls": 0,
        "ops": [],          # interleaved send/mark operations
        "failed": [],
        "released": [],
        "stuck": 0,
        "swept": 0,
        # rotation pool: None -> always yield the default sender; a list ->
        # a queue popped per pick; "empty" -> None (pool at cap).
        "pool": None,
        "sender_claims": 0,
        "sender_releases": [],
        # pinned picks (claim_pinned_sender): sender_id -> sender dict |
        # "empty" (that mailbox at cap -> None) | list queue. An id with no
        # entry yields a deterministic stand-in sender.
        "pinned": {},
        "pinned_claims": [],
        "synced": None,     # what sync_senders_from_mailjet was called with
    }

    async def claim_email_batch(limit=None):
        s["claim_calls"] += 1
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

    async def claim_rotating_sender():
        s["sender_claims"] += 1
        p = s["pool"]
        if p == "empty":
            return None
        if isinstance(p, list):
            return p.pop(0) if p else None
        return {"sender_email": "pool@x.com", "sender_name": "Pool"}

    async def claim_pinned_sender(sender_id):
        s["pinned_claims"].append(sender_id)
        p = s["pinned"].get(sender_id, {"sender_email": f"pin{sender_id}@x.com",
                                        "sender_name": f"Pin{sender_id}"})
        if p == "empty":
            return None
        if isinstance(p, list):
            return p.pop(0) if p else None
        return p

    async def release_rotating_sender(email):
        s["sender_releases"].append(email)
        return True

    async def sync_senders_from_mailjet(records, default_cap):
        s["synced"] = {"records": records, "default_cap": default_cap}
        return {"inserted": len(records), "deactivated": 0}

    for fn in (claim_email_batch, mark_email_sent, mark_email_failed,
               release_email_claims, count_stuck_sending, sweep_suppressed,
               claim_rotating_sender, claim_pinned_sender,
               release_rotating_sender, sync_senders_from_mailjet):
        monkeypatch.setattr(repo, fn.__name__, fn)

    return s


@pytest.mark.asyncio
async def test_marks_each_send_immediately_not_batched(state):
    """The write follows every vendor accept before the next send starts —
    a crash can never lose more than the one in-flight contact."""
    state["claims"] = [[target(1), target(2)]]

    stats = await emailer.run(FakeSender(state["ops"], ["r1", "r2"]))

    assert state["ops"] == ["sweep",
                            "send:1", "mark:1:r1",
                            "send:2", "mark:2:r2"]
    assert stats.sent == 2


@pytest.mark.asyncio
async def test_sweep_runs_before_any_claim(state):
    state["claims"] = [[target(1)]]
    state["swept"] = 3

    stats = await emailer.run(FakeSender(state["ops"]))

    assert state["ops"][0] == "sweep"
    assert stats.suppressed == 3


@pytest.mark.asyncio
async def test_rejection_marks_failed_and_continues(state):
    sender = FakeSender(state["ops"], [SendRejected("bad address"), "r2"])
    state["claims"] = [[target(1), target(2)]]

    stats = await emailer.run(sender)

    assert state["failed"] == [(1, "mailjet", "bad address")]
    assert "mark:2:r2" in state["ops"]
    assert stats.rejected == 1
    assert stats.sent == 1


@pytest.mark.asyncio
async def test_vendor_down_releases_current_and_rest_keeps_prior_sends(state):
    sender = FakeSender(state["ops"], ["r1", ProviderError("mailjet: gave up")])
    state["claims"] = [[target(1), target(2), target(3)]]

    stats = await emailer.run(sender)

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
    sender = FakeSender(state["ops"], ["r1", SendUncertain("mailjet: ambiguous")])
    state["claims"] = [[target(1), target(2), target(3)]]

    stats = await emailer.run(sender)

    assert "mark:1:r1" in state["ops"]   # the committed send stands
    assert state["failed"] == []         # the uncertain contact is NOT failed
    assert state["released"] == [[3]]    # only the untried remainder released;
                                         # contact 2 is left at 'sending'
    assert stats.uncertain == 1
    assert stats.sent == 1
    assert stats.errors


@pytest.mark.asyncio
async def test_stuck_sending_is_surfaced_never_reset(state):
    state["stuck"] = 4
    stats = await emailer.run(FakeSender(state["ops"]))
    assert stats.stuck_sending == 4
    assert state["released"] == []  # nothing touched


@pytest.mark.asyncio
async def test_no_sender_configured_claims_nothing(state):
    # Mailjet keys unset (conftest default) -> build_sender() is None.
    stats = await emailer.run()
    assert stats.claimed == 0
    assert state["claim_calls"] == 0     # never reached the claim loop
    assert "sweep" in state["ops"]       # sweep still runs first


@pytest.mark.asyncio
async def test_multi_pass_drain_and_cap(state, monkeypatch):
    monkeypatch.setattr(config, "SEND_BATCH_SIZE", 1)
    state["claims"] = [[target(i)] for i in range(1, 6)]

    stats = await emailer.run(FakeSender(state["ops"]), max_passes=3)

    assert stats.passes == 3
    assert stats.sent == 3


def test_build_sender_returns_mailjet_when_configured(monkeypatch):
    from app.providers.mailjet import MailjetSender
    monkeypatch.setattr(config, "MAILJET_API_KEY", "k")
    monkeypatch.setattr(config, "MAILJET_SECRET_KEY", "s")
    assert isinstance(emailer.build_sender(), MailjetSender)


def test_build_sender_is_none_without_keys():
    assert emailer.build_sender() is None


# ---- sender rotation --------------------------------------------------


@pytest.mark.asyncio
async def test_every_send_rotates_the_from_through_the_pool(state):
    """Each send draws the next pool identity and it lands on the target the
    sender receives (via dataclasses.replace)."""
    cap = CapturingSender()
    state["pool"] = [{"sender_email": "a@d1.com", "sender_name": "A"},
                     {"sender_email": "b@d2.com", "sender_name": "B"}]
    state["claims"] = [[target(1), target(2)]]

    await emailer.run(cap)

    assert cap.seen == [(1, "a@d1.com", "A"), (2, "b@d2.com", "B")]


@pytest.mark.asyncio
async def test_exhausted_pool_pauses_the_run_and_releases_the_batch(state):
    """A None pick means every domain is at its daily cap: send nothing,
    release the whole claimed batch, stop (it resumes when caps reset)."""
    state["pool"] = "empty"
    state["claims"] = [[target(1), target(2), target(3)]]

    stats = await emailer.run(FakeSender(state["ops"]))

    assert stats.sent == 0
    assert state["released"] == [[1, 2, 3]]
    assert any("daily cap" in e for e in stats.errors)


@pytest.mark.asyncio
async def test_send_rejected_returns_the_rotating_slot(state):
    """A hard rejection means the mail never left — the domain's daily
    count must be given back so a bad address doesn't burn capacity."""
    state["pool"] = [{"sender_email": "a@d1.com", "sender_name": "A"}]
    state["claims"] = [[target(1)]]

    await emailer.run(FakeSender(state["ops"], [SendRejected("bad")]))

    assert state["sender_releases"] == ["a@d1.com"]
    assert state["failed"] == [(1, "mailjet", "bad")]


@pytest.mark.asyncio
async def test_uncertain_send_keeps_the_rotating_slot(state):
    """An uncertain send may have landed — the count must stand (matches
    leaving the contact at 'sending'), so no slot is returned."""
    state["pool"] = [{"sender_email": "a@d1.com", "sender_name": "A"}]
    state["claims"] = [[target(1), target(2)]]

    await emailer.run(FakeSender(state["ops"], [SendUncertain("maybe")]))

    assert state["sender_releases"] == []


@pytest.mark.asyncio
async def test_provider_error_returns_only_the_current_slot(state):
    """Only the current contact drew a sender; the released remainder never
    picked one, so exactly one slot is returned."""
    state["pool"] = [{"sender_email": "a@d1.com", "sender_name": "A"},
                     {"sender_email": "b@d2.com", "sender_name": "B"}]
    state["claims"] = [[target(1), target(2), target(3)]]

    await emailer.run(FakeSender(state["ops"], ["r1", ProviderError("down")]))

    assert state["sender_releases"] == ["b@d2.com"]   # only contact 2's slot
    assert state["released"] == [[2, 3]]


# ---- single-mailbox pin (per-campaign) --------------------------------


@pytest.mark.asyncio
async def test_pinned_campaign_draws_only_its_mailbox_never_the_pool(state):
    """A contact whose campaign is pinned sends from that one sender via
    claim_pinned_sender; the rotation pool is never consulted for it."""
    cap = CapturingSender()
    state["pinned"] = {5: {"sender_email": "one@pinned.com", "sender_name": "One"}}
    state["claims"] = [[target(1, pinned_sender_id=5)]]

    await emailer.run(cap)

    assert cap.seen == [(1, "one@pinned.com", "One")]
    assert state["pinned_claims"] == [5]     # drew the pinned sender
    assert state["sender_claims"] == 0       # and never the rotation pool


@pytest.mark.asyncio
async def test_pinned_at_cap_releases_only_that_contact_and_keeps_going(state):
    """A pinned mailbox at its daily cap must NOT pause the whole run — the
    capped contact is returned to 'drafted' and the rest of the batch (here
    a rotating contact) still sends. This is why a pinned pick releases one
    contact and continues, where an exhausted pool pauses everything."""
    cap = CapturingSender()
    state["pinned"] = {5: "empty"}           # campaign 5's mailbox is at cap
    state["claims"] = [[target(1, pinned_sender_id=5), target(2)]]

    stats = await emailer.run(cap)

    assert state["released"] == [[1]]        # only the capped pinned contact
    assert cap.seen == [(2, "pool@x.com", "Pool")]   # the rotating one sent
    assert stats.sent == 1
    assert stats.released == 1


@pytest.mark.asyncio
async def test_pinned_rejection_returns_the_pinned_slot(state):
    """A hard rejection means the mail never left; the pinned mailbox's daily
    count is handed back (same release path as rotation, keyed on email)."""
    state["pinned"] = {5: {"sender_email": "one@pinned.com", "sender_name": "One"}}
    state["claims"] = [[target(1, pinned_sender_id=5)]]

    await emailer.run(FakeSender(state["ops"], [SendRejected("bad")]))

    assert state["sender_releases"] == ["one@pinned.com"]
    assert state["failed"] == [(1, "mailjet", "bad")]


# ---- pool auto-enrol from Mailjet (sync_pool) -------------------------


class SyncingSender:
    """A sender that can list verified records (like MailjetSender), so
    run()/sync_pool actually reconcile the pool."""
    name = "mailjet"

    def __init__(self, records, ops=None):
        self._records = records
        self._ops = ops if ops is not None else []

    async def list_verified_sender_records(self):
        return self._records

    async def send(self, t):
        self._ops.append(f"send:{t.id}")
        return "ref"


@pytest.mark.asyncio
async def test_run_syncs_the_pool_from_mailjet_before_claiming(state):
    """A domain verified in Mailjet is usable this pass with no manual step:
    run() reconciles the pool from the sender's verified list first, at the
    configured default cap."""
    records = [{"email": "a@d1.com", "name": "A"}]
    state["claims"] = []   # nothing to send; assert only that the sync ran
    await emailer.run(SyncingSender(records))
    assert state["synced"] == {"records": records,
                               "default_cap": config.MAILJET_SENDER_DAILY_CAP}


@pytest.mark.asyncio
async def test_sync_pool_is_a_noop_when_sender_cannot_list(state):
    """A basic/fake sender (no list_verified_sender_records) never reaches
    the DB — sync degrades to a no-op so the send loop is unaffected. This
    is exactly what keeps the fakes-only emailer tests from touching repo."""
    result = await emailer.sync_pool(FakeSender(state["ops"]))
    assert result["error"] == "sender pool sync unavailable"
    assert state["synced"] is None


@pytest.mark.asyncio
async def test_sync_pool_swallows_a_mailjet_outage_without_touching_the_pool(state):
    """A ProviderError from the verified-list read must NOT reconcile against
    an empty list (which would pause the whole pool). It short-circuits
    before repo.sync_senders_from_mailjet, leaving the last-synced pool."""
    class DeadSender:
        name = "mailjet"

        async def list_verified_sender_records(self):
            raise ProviderError("mailjet: sender list unreachable")

        async def send(self, t):
            return "ref"

    result = await emailer.sync_pool(DeadSender())
    assert "unreachable" in result["error"]
    assert state["synced"] is None   # repo.sync never called
