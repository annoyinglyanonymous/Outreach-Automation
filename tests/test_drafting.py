"""Drafting stage tests — fakes only, no database, no Claude API.

Locks in the two-path split (template contacts never cost an LLM call),
the 300-char note constraint handling, and the same vendor-failure
semantics as the other stages.
"""
from __future__ import annotations

import pytest

from app import drafting, repo
from app.config import config
from app.providers.base import Draft, DraftRefused, ProviderError
from app.repo import DraftTarget


# ---------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------


class FakeDrafter:
    def __init__(self, queue=None):
        self.calls: list[tuple[str, str]] = []
        self.queue = list(queue or [])

    async def draft(self, system, user):
        self.calls.append((system, user))
        item = self.queue.pop(0) if self.queue else Draft("Subject", "Body", "Note")
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def state(monkeypatch):
    s = {
        "claims": [],     # queue of batches for claim_draft_batch
        "written": [],    # write_drafts payloads
        "released": [],   # ids passed to release_draft_claims
        "stale": 0,
    }

    async def claim_draft_batch(limit=None):
        return s["claims"].pop(0) if s["claims"] else []

    async def write_drafts(results):
        s["written"].append(results)
        return len(results)

    async def release_draft_claims(ids):
        s["released"].append(list(ids))
        return len(ids)

    async def reset_stale_draft_claims():
        return s["stale"]

    for fn in (claim_draft_batch, write_drafts, release_draft_claims,
               reset_stale_draft_claims):
        monkeypatch.setattr(repo, fn.__name__, fn, raising=False)

    return s


CAMPAIGN = {
    "offer": "Renegade back-office support",
    "cta": "open to a 15-minute call next week?",
    "tone": "plain, direct",
    "sender": "Rojan",
    "audience_rationale": "independent agents drowning in paperwork",
    "fallback_email_subject": "Quick question, {{first_name}}",
    "fallback_email_body": "Hi {{first_name}}, saw {{company}} and thought of you. - {{sender}}",
}


def target(id=1, profile=None, **overrides) -> DraftTarget:
    fields = {
        "id": id,
        "email": f"agent{id}@example.com",
        "first_name": "Jane",
        "last_name": "Doe",
        "company": "Doe Insurance",
        "title": "Agency Owner",
        "linkedin_url": "https://linkedin.com/in/jane-doe",
        "profile_data": profile,
        "campaign": CAMPAIGN,
    }
    fields.update(overrides)
    return DraftTarget(**fields)


PROFILE = {"headline": "Agency Owner at Doe Insurance", "summary": "20 years in P&C"}


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------


def test_render_template_fills_merge_fields():
    fields = drafting.merge_fields(target())
    body = drafting.render_template(CAMPAIGN["fallback_email_body"], fields)
    assert body == "Hi Jane, saw Doe Insurance and thought of you. - Rojan"


def test_render_template_drops_unknown_fields():
    assert drafting.render_template("Hi {{nope}}!", {}) == "Hi !"


def test_clamp_note_respects_limit_at_word_boundary():
    long_note = "word " * 100
    clamped = drafting.clamp_note(long_note)
    assert len(clamped) <= drafting.NOTE_MAX_CHARS
    assert not clamped.rstrip(".").endswith("wor")  # no mid-word cut


def test_clamp_note_passes_short_notes_through():
    assert drafting.clamp_note("short") == "short"
    assert drafting.clamp_note(None) is None


def test_build_prompts_truncates_huge_profiles(monkeypatch):
    monkeypatch.setattr(config, "DRAFT_PROFILE_CHAR_LIMIT", 200)
    _, user = drafting.build_prompts(target(profile={"blob": "x" * 10_000}))
    assert len(user) < 600


# ---------------------------------------------------------------------
# runner: two paths
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_template_path_never_calls_the_llm(state):
    drafter = FakeDrafter()
    state["claims"] = [[target(1, profile=None), target(2, profile=None)]]

    stats = await drafting.run(drafter)

    assert drafter.calls == []
    assert stats.template_drafted == 2
    assert stats.llm_drafted == 0
    (written,) = state["written"]
    assert all(r["path"] == "template" for r in written)
    assert all(r["linkedin_note"] is None for r in written)
    assert written[0]["email_subject"] == "Quick question, Jane"


@pytest.mark.asyncio
async def test_profile_path_uses_the_llm(state):
    drafter = FakeDrafter([Draft("Subj", "Body text", "A short note")])
    state["claims"] = [[target(1, profile=PROFILE)]]

    stats = await drafting.run(drafter)

    assert len(drafter.calls) == 1
    system, user = drafter.calls[0]
    assert "Renegade back-office support" in system
    assert "Doe Insurance" in user
    assert stats.llm_drafted == 1
    (written,) = state["written"]
    assert written[0] == {
        "id": 1,
        "email_subject": "Subj",
        "email_body": "Body text",
        "linkedin_note": "A short note",
        "path": "llm",
    }


@pytest.mark.asyncio
async def test_mixed_batch_splits_paths(state):
    drafter = FakeDrafter([Draft("S", "B", "N")])
    state["claims"] = [[target(1, profile=PROFILE), target(2, profile=None)]]

    stats = await drafting.run(drafter)

    assert stats.llm_drafted == 1
    assert stats.template_drafted == 1
    assert len(drafter.calls) == 1


# ---------------------------------------------------------------------
# runner: note length handling
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overlong_note_triggers_one_retry(state):
    drafter = FakeDrafter([
        Draft("S", "B", "x" * 400),      # too long
        Draft("S", "B", "short note"),   # corrected
    ])
    state["claims"] = [[target(1, profile=PROFILE)]]

    await drafting.run(drafter)

    assert len(drafter.calls) == 2
    assert "too long" in drafter.calls[1][1]
    (written,) = state["written"]
    assert written[0]["linkedin_note"] == "short note"


@pytest.mark.asyncio
async def test_note_overlong_twice_is_clamped(state):
    drafter = FakeDrafter([
        Draft("S", "B", "word " * 100),
        Draft("S", "B", "word " * 100),
    ])
    state["claims"] = [[target(1, profile=PROFILE)]]

    await drafting.run(drafter)

    (written,) = state["written"]
    assert len(written[0]["linkedin_note"]) <= drafting.NOTE_MAX_CHARS


# ---------------------------------------------------------------------
# runner: failure semantics
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_failure_keeps_paid_work_and_releases_rest(state):
    """Vendor down mid-batch: drafts already paid for are written, the
    unprocessed remainder (current contact included) goes back to the
    queue, and the run aborts instead of re-claiming and failing again."""
    drafter = FakeDrafter([
        Draft("S1", "B1", "N1"),
        ProviderError("anthropic: gave up"),
    ])
    state["claims"] = [[
        target(1, profile=PROFILE),
        target(2, profile=None),          # template, processed before failure
        target(3, profile=PROFILE),       # fails here
        target(4, profile=PROFILE),       # never attempted
    ]]

    stats = await drafting.run(drafter)

    assert state["released"] == [[3, 4]]
    (written,) = state["written"]
    assert [r["id"] for r in written] == [1, 2]
    assert stats.errors
    assert stats.released == 2


@pytest.mark.asyncio
async def test_refusal_releases_one_contact_and_continues(state):
    drafter = FakeDrafter([
        DraftRefused("declined"),
        Draft("S", "B", "N"),
    ])
    state["claims"] = [[target(1, profile=PROFILE), target(2, profile=PROFILE)]]

    stats = await drafting.run(drafter)

    assert state["released"] == [[1]]
    (written,) = state["written"]
    assert [r["id"] for r in written] == [2]
    assert stats.refused == 1
    assert stats.llm_drafted == 1


# ---------------------------------------------------------------------
# runner: queue mechanics
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_pass_drains_queue(state, monkeypatch):
    monkeypatch.setattr(config, "DRAFT_BATCH_SIZE", 2)
    state["claims"] = [
        [target(1, profile=None), target(2, profile=None)],  # full batch
        [target(3, profile=None)],                           # short: drained
    ]

    stats = await drafting.run(FakeDrafter())

    assert stats.passes == 2
    assert stats.claimed == 3


@pytest.mark.asyncio
async def test_pass_cap_bounds_a_run(state, monkeypatch):
    monkeypatch.setattr(config, "DRAFT_BATCH_SIZE", 1)
    state["claims"] = [[target(i, profile=None)] for i in range(1, 11)]

    stats = await drafting.run(FakeDrafter(), max_passes=3)

    assert stats.passes == 3


@pytest.mark.asyncio
async def test_stale_claims_recovered(state):
    state["stale"] = 2
    stats = await drafting.run(FakeDrafter())
    assert stats.stale_recovered == 2
