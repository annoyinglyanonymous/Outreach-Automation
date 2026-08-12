"""Enrichment runner tests — fakes only, no database, no Apollo.

Formalises the semantics the README lists as still living in throwaway
scripts. The two that cost real money if they regress:

- a vendor failure releases claims and aborts; it is never written as a
  contact outcome ("the vendor was down" is not "this person has no
  LinkedIn"),
- a genuine miss IS written as an outcome — url NULL, contact moves
  forward — because status means "where in the pipeline", never "what
  the outcome was".
"""
from __future__ import annotations

import pytest

from app import repo, runner
from app.config import config
from app.providers.base import EnrichmentResult, ProviderError


# ---------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------


class FakeProvider:
    """Stands in for ApolloProvider. Queue entries are either a list of
    EnrichmentResult or an exception to raise."""

    name = "apollo"
    batch_size = 10

    def __init__(self, queue=None):
        self.queue = list(queue or [])
        self.seen: list[list[str]] = []

    async def enrich(self, contacts):
        self.seen.append([c.email for c in contacts])
        item = self.queue.pop(0) if self.queue else []
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def state(patch_repo):
    s = {
        "claims": [],      # queue of batches handed out by claim_batch
        "cache": {},       # email -> (url, confidence)
        "written": [],     # write_results payloads
        "released": [],    # release_claims id lists
        "stale": 0,
    }

    async def claim_batch(limit=None):
        return s["claims"].pop(0) if s["claims"] else []

    async def cache_lookup(emails):
        return {e: s["cache"][e] for e in emails if e in s["cache"]}

    async def write_results(results):
        s["written"].append(results)
        return len(results)

    async def release_claims(ids):
        s["released"].append(list(ids))
        return len(ids)

    async def reset_stale_claims():
        return s["stale"]

    patch_repo(claim_batch, cache_lookup, write_results, release_claims,
               reset_stale_claims)
    return s


def contacts(*emails: str) -> list[repo.Contact]:
    return [
        repo.Contact(id=i + 1, email=e, first_name="Jane", last_name="Doe",
                     company="Doe Insurance", title="Agent")
        for i, e in enumerate(emails)
    ]


def written_by_id(state) -> dict[int, dict]:
    return {r["id"]: r for batch in state["written"] for r in batch}


# ---------------------------------------------------------------------
# tier 0: cache
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_skips_the_paid_lookup(state):
    """Tier 0 is free; a cached contact must never reach the provider."""
    state["claims"] = [contacts("a@x.com")]
    state["cache"] = {"a@x.com": ("https://linkedin.com/in/a", 0.9)}
    provider = FakeProvider()

    stats = await runner.run(provider)

    assert provider.seen == []          # no paid call at all
    assert stats.from_cache == 1
    assert stats.from_provider == 0
    assert written_by_id(state)[1]["tier"] == "cache"


@pytest.mark.asyncio
async def test_only_misses_are_sent_to_the_provider(state):
    state["claims"] = [contacts("hit@x.com", "miss@x.com")]
    state["cache"] = {"hit@x.com": ("https://linkedin.com/in/hit", 0.9)}
    provider = FakeProvider([[
        EnrichmentResult("miss@x.com", "https://linkedin.com/in/miss", 0.8, "apollo"),
    ]])

    stats = await runner.run(provider)

    assert provider.seen == [["miss@x.com"]]
    assert stats.from_cache == 1
    assert stats.from_provider == 1


# ---------------------------------------------------------------------
# outcomes vs failures
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolved_contact_is_written_forward_not_released(state):
    """Apollo had no match: that is an outcome. The contact is written
    with a NULL url (drafting will use the template) — releasing it would
    strand it in the queue forever, re-paying on every pass."""
    state["claims"] = [contacts("nobody@x.com")]
    provider = FakeProvider([[]])       # provider returns no match at all

    stats = await runner.run(provider)

    assert state["released"] == []
    row = written_by_id(state)[1]
    assert row["linkedin_url"] is None
    assert row["linkedin_confidence"] == 0.0
    assert row["tier"] == "apollo"
    assert stats.unresolved == 1


@pytest.mark.asyncio
async def test_provider_failure_releases_claims_and_aborts(state):
    """429/5xx/timeout is the vendor's failure, not the contacts'. They go
    back to 'pending' and the run stops rather than burning the next batch
    against a provider that is already down."""
    state["claims"] = [contacts("a@x.com", "b@x.com"), contacts("c@x.com")]
    provider = FakeProvider([ProviderError("apollo: gave up")])

    stats = await runner.run(provider)

    assert state["released"] == [[1, 2]]
    assert stats.released == 2
    assert stats.errors
    assert stats.passes == 1
    assert provider.seen == [["a@x.com", "b@x.com"]]   # second batch untouched
    assert state["claims"]                             # and never claimed


@pytest.mark.asyncio
async def test_provider_failure_still_persists_the_cache_hits(state):
    """The free tier already resolved these; discarding them would re-pay
    nothing but would lose work that cost a query."""
    state["claims"] = [contacts("hit@x.com", "miss@x.com")]
    state["cache"] = {"hit@x.com": ("https://linkedin.com/in/hit", 0.9)}
    provider = FakeProvider([ProviderError("apollo: gave up")])

    await runner.run(provider)

    assert state["released"] == [[2]]           # only the miss goes back
    written = written_by_id(state)
    assert written[1]["linkedin_url"] == "https://linkedin.com/in/hit"
    assert 2 not in written                     # the miss was not written


@pytest.mark.asyncio
async def test_nothing_is_written_when_a_run_finds_no_work(state):
    stats = await runner.run(FakeProvider())
    assert state["written"] == []
    assert stats.claimed == 0
    assert stats.passes == 0


# ---------------------------------------------------------------------
# loop control — the spend bounds
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_passes_caps_a_run(state, monkeypatch):
    """MAX_PASSES exists so a bug cannot spin forever burning API credit."""
    monkeypatch.setattr(type(config), "BATCH_SIZE", 2)
    state["claims"] = [contacts(f"c{i}@x.com", f"d{i}@x.com") for i in range(10)]
    provider = FakeProvider()

    stats = await runner.run(provider, max_passes=3)

    assert stats.passes == 3
    assert len(provider.seen) == 3
    assert state["claims"]      # work remains for the next trigger


@pytest.mark.asyncio
async def test_short_batch_ends_the_run(state, monkeypatch):
    """A batch smaller than BATCH_SIZE means the queue is drained."""
    monkeypatch.setattr(type(config), "BATCH_SIZE", 10)
    state["claims"] = [contacts("a@x.com"), contacts("b@x.com")]

    stats = await runner.run(FakeProvider())

    assert stats.passes == 1
    assert state["claims"]      # the second batch was never claimed


@pytest.mark.asyncio
async def test_stale_claims_are_recovered_before_claiming(state):
    """Rows a crashed run left at 'enriching' are invisible to the queue
    and nothing errored, so without this they sit stranded forever."""
    state["stale"] = 3
    stats = await runner.run(FakeProvider())
    assert stats.stale_recovered == 3


# ---------------------------------------------------------------------
# provider selection
# ---------------------------------------------------------------------


def test_build_provider_follows_config(monkeypatch):
    from app.providers.apollo import ApolloProvider

    monkeypatch.setattr(type(config), "PROVIDER", "apollo")
    assert isinstance(runner.build_provider(), ApolloProvider)


def test_unknown_provider_is_a_startup_error(monkeypatch):
    monkeypatch.setattr(type(config), "PROVIDER", "clearbit")
    with pytest.raises(RuntimeError, match="Unknown PROVIDER"):
        runner.build_provider()
