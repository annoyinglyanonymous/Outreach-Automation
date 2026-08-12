"""Verify runner — fakes only. Applies the right verdict per match, drains
the queue, stops on a provider error, and no-ops with no LLM configured."""
from __future__ import annotations

import pytest

from app import repo, verification, verifier
from app.providers.base import ProviderError
from app.verification import Verdict


def targets(ids):
    return [
        repo.VerifyTarget(
            id=i, email=f"c{i}@x.com", first_name="C", last_name=str(i),
            company="Co", title="Owner",
            linkedin_url="https://linkedin.com/in/c", profile_data={"headline": "h"},
        )
        for i in ids
    ]


@pytest.fixture
def rec(monkeypatch):
    state = {"confirmed": [], "rejected": []}

    async def confirm(cid, by, reason=None):
        state["confirmed"].append((cid, by, reason))
        return True

    async def reject(cid, by, reason=None):
        state["rejected"].append((cid, by, reason))
        return True

    monkeypatch.setattr(repo, "confirm_enrichment", confirm)
    monkeypatch.setattr(repo, "reject_enrichment", reject)
    return state


def one_batch(ids, monkeypatch):
    """Queue that yields `ids` once, then drains (empty once anything is
    excluded) — mirrors how verdict events drop contacts from the queue."""
    async def queue(exclude_ids, limit=None):
        return [] if exclude_ids else targets(ids)
    monkeypatch.setattr(repo, "verify_queue_batch", queue)


def fixed_verdicts(mapping, monkeypatch):
    async def verify_match(target, verifier_):
        return mapping[target.id]
    monkeypatch.setattr(verification, "verify_match", verify_match)


@pytest.mark.asyncio
async def test_applies_verdict_per_contact(rec, monkeypatch):
    one_batch([1, 2], monkeypatch)
    fixed_verdicts({
        1: Verdict("right_person", 0.9, "same company"),
        2: Verdict("wrong_person", 0.8, "different company"),
    }, monkeypatch)

    stats = await verifier.run(verifier="fake")  # non-None skips build_verifier
    assert stats.confirmed == 1 and stats.rejected == 1
    assert [c[0] for c in rec["confirmed"]] == [1]
    assert [r[0] for r in rec["rejected"]] == [2]
    assert rec["confirmed"][0][1] == "ai"                 # reviewed_by
    assert rec["confirmed"][0][2].startswith("AI: right_person")  # audit reason


@pytest.mark.asyncio
async def test_unsure_is_rejected(rec, monkeypatch):
    one_batch([1], monkeypatch)
    fixed_verdicts({1: Verdict("unsure", 0.3, "thin evidence")}, monkeypatch)

    stats = await verifier.run(verifier="fake")
    assert stats.rejected == 1 and stats.confirmed == 0


@pytest.mark.asyncio
async def test_provider_error_stops_run(rec, monkeypatch):
    one_batch([1, 2], monkeypatch)

    async def verify_match(target, verifier_):
        raise ProviderError("n8n down")
    monkeypatch.setattr(verification, "verify_match", verify_match)

    stats = await verifier.run(verifier="fake")
    assert stats.checked == 0                     # aborted before any verdict
    assert stats.errors and "down" in stats.errors[0]
    assert not rec["confirmed"] and not rec["rejected"]


@pytest.mark.asyncio
async def test_no_provider_is_a_noop(monkeypatch):
    monkeypatch.setattr(verification, "build_verifier", lambda: None)

    async def boom(*a, **k):
        raise AssertionError("must not claim work with no provider configured")
    monkeypatch.setattr(repo, "verify_queue_batch", boom)

    stats = await verifier.run()   # no verifier passed -> build_verifier() -> None
    assert stats.passes == 0 and stats.checked == 0
