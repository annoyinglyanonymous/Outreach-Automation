"""Scraper stage tests — fakes only, no database, no Apify.

The behaviours locked in here mirror the ones that mattered in the
enrichment runner: vendor failure must never be recorded as a contact
outcome, and nothing may silently discard reachable people.
"""
from __future__ import annotations

import pytest

from app import repo, scraper
from app.config import config
from app.providers.apify import ApifyClient, RunInfo
from app.providers.base import ProviderError
from app.repo import ScrapeTarget


# ---------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------


class FakeClient:
    """Stands in for ApifyClient; runs/items are plain dicts."""

    def __init__(self):
        self.runs: dict[str, RunInfo] = {}
        self.items: dict[str, list[dict]] = {}
        self.started: list[list[str]] = []
        self.start_error: Exception | None = None
        self.get_error: Exception | None = None
        self._next = 0

    async def start_run(self, urls):
        if self.start_error:
            raise self.start_error
        self._next += 1
        run_id = f"run{self._next}"
        self.started.append(list(urls))
        return run_id

    async def get_run(self, run_id):
        if self.get_error:
            raise self.get_error
        return self.runs[run_id]

    async def fetch_items(self, dataset_id):
        return self.items[dataset_id]


@pytest.fixture
def state(monkeypatch):
    """Replace every repo function the scraper touches with an in-memory fake."""
    # Pin the URL-field list: config loads .env at import, so without this
    # the tests would change behaviour based on the developer's local
    # actor configuration.
    monkeypatch.setattr(
        config, "APIFY_URL_FIELDS",
        ("url", "profileUrl", "linkedinUrl", "inputUrl", "publicUrl"),
    )
    s = {
        "claims": [],          # queue of batches handed out by claim_scrape_batch
        "run_contacts": {},    # run_id -> [ScrapeTarget]
        "pending": [],         # run ids awaiting collection
        "written": [],         # write_profiles payloads
        "released_runs": [],
        "released_ids": [],
        "run_ids_set": [],     # (run_id, ids) from set_run_id
        "stale": 0,
    }

    async def claim_scrape_batch(limit=None):
        return s["claims"].pop(0) if s["claims"] else []

    async def set_run_id(run_id, ids):
        s["run_ids_set"].append((run_id, list(ids)))
        return len(ids)

    async def pending_runs():
        return list(s["pending"])

    async def run_contacts(run_id):
        return s["run_contacts"].get(run_id, [])

    async def write_profiles(results):
        s["written"].append(results)
        return len(results)

    async def release_run(run_id):
        s["released_runs"].append(run_id)
        return len(s["run_contacts"].get(run_id, []))

    async def release_scrape_claims(ids):
        s["released_ids"].append(list(ids))
        return len(ids)

    async def reset_stale_scrape_claims():
        return s["stale"]

    for fn in (claim_scrape_batch, set_run_id, pending_runs, run_contacts,
               write_profiles, release_run, release_scrape_claims,
               reset_stale_scrape_claims):
        monkeypatch.setattr(repo, fn.__name__, fn)

    return s


def targets(*urls: str) -> list[ScrapeTarget]:
    return [ScrapeTarget(id=i + 1, linkedin_url=u) for i, u in enumerate(urls)]


# ---------------------------------------------------------------------
# URL canonicalisation
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://www.linkedin.com/in/jane-doe/", "http://linkedin.com/in/jane-doe"),
        ("https://np.linkedin.com/in/jane-doe", "https://www.linkedin.com/in/jane-doe"),
        ("https://linkedin.com/in/jane-doe?trk=abc#top", "linkedin.com/in/jane-doe"),
        ("https://WWW.LinkedIn.com/in/Jane-Doe", "https://linkedin.com/in/jane-doe"),
    ],
)
def test_canonical_url_equates_profile_variants(left, right):
    assert scraper.canonical_url(left) == scraper.canonical_url(right)


def test_canonical_url_distinguishes_different_profiles():
    a = scraper.canonical_url("https://linkedin.com/in/jane-doe")
    b = scraper.canonical_url("https://linkedin.com/in/jane-doe-12345")
    assert a != b


def test_canonical_url_handles_empty():
    assert scraper.canonical_url(None) == ""
    assert scraper.canonical_url("") == ""


# ---------------------------------------------------------------------
# collector
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_succeeded_run_writes_profiles(state):
    client = FakeClient()
    client.runs["r1"] = RunInfo("r1", "SUCCEEDED", "ds1")
    client.items["ds1"] = [
        {"url": "https://www.linkedin.com/in/a", "headline": "Agent A"},
        {"url": "https://linkedin.com/in/b/", "headline": "Agent B"},
    ]
    state["pending"] = ["r1"]
    state["run_contacts"]["r1"] = targets(
        "https://linkedin.com/in/a", "https://www.linkedin.com/in/b"
    )

    stats = await scraper.run(client)

    assert stats.runs_collected == 1
    assert stats.profiles_written == 2
    assert stats.scrape_failed == 0
    (written,) = state["written"]
    assert all(r["profile"] is not None for r in written)
    assert all(r["run_id"] == "r1" for r in written)


@pytest.mark.asyncio
async def test_profile_missing_from_dataset_is_an_outcome(state):
    """A profile the actor could not scrape still moves forward — the
    failure is data (profile=None), never a stuck status."""
    client = FakeClient()
    client.runs["r1"] = RunInfo("r1", "SUCCEEDED", "ds1")
    client.items["ds1"] = [{"url": "https://linkedin.com/in/a", "headline": "A"}]
    state["pending"] = ["r1"]
    state["run_contacts"]["r1"] = targets(
        "https://linkedin.com/in/a", "https://linkedin.com/in/gone"
    )

    stats = await scraper.run(client)

    assert stats.profiles_written == 1
    assert stats.scrape_failed == 1
    (written,) = state["written"]
    by_id = {r["id"]: r["profile"] for r in written}
    assert by_id[1] is not None
    assert by_id[2] is None


@pytest.mark.asyncio
async def test_failed_run_releases_contacts_for_rescrape(state):
    """FAILED/TIMED-OUT/ABORTED is the vendor's failure, not the contacts' —
    they go back to 'enriched', never forward without profiles."""
    client = FakeClient()
    client.runs["r1"] = RunInfo("r1", "FAILED", None)
    state["pending"] = ["r1"]
    state["run_contacts"]["r1"] = targets("https://linkedin.com/in/a")

    stats = await scraper.run(client)

    assert state["released_runs"] == ["r1"]
    assert state["written"] == []
    assert stats.runs_failed == 1
    assert stats.released == 1


@pytest.mark.asyncio
async def test_expired_run_treated_as_failed(state):
    client = FakeClient()
    client.runs["r1"] = RunInfo("r1", "MISSING", None)
    state["pending"] = ["r1"]
    state["run_contacts"]["r1"] = targets("https://linkedin.com/in/a")

    await scraper.run(client)

    assert state["released_runs"] == ["r1"]


@pytest.mark.asyncio
async def test_in_flight_run_left_alone_and_occupies_a_slot(state, monkeypatch):
    monkeypatch.setattr(config, "APIFY_MAX_ACTIVE_RUNS", 1)
    client = FakeClient()
    client.runs["r1"] = RunInfo("r1", "RUNNING", None)
    state["pending"] = ["r1"]
    state["claims"] = [targets("https://linkedin.com/in/x")]

    stats = await scraper.run(client)

    assert state["released_runs"] == []
    assert state["written"] == []
    assert client.started == []  # slot occupied, starter must not launch
    assert stats.in_flight == 1


@pytest.mark.asyncio
async def test_status_check_error_leaves_run_claimed(state, monkeypatch):
    """Apify unreachable ≠ run failed: nothing released, nothing written,
    and the unknown run still counts against the concurrency cap."""
    monkeypatch.setattr(config, "APIFY_MAX_ACTIVE_RUNS", 1)
    client = FakeClient()
    client.get_error = ProviderError("apify: gave up")
    state["pending"] = ["r1"]
    state["claims"] = [targets("https://linkedin.com/in/x")]

    stats = await scraper.run(client)

    assert state["released_runs"] == []
    assert state["written"] == []
    assert client.started == []
    assert stats.errors


@pytest.mark.asyncio
async def test_zero_matches_with_nonempty_dataset_blocks_write(state):
    """Dataset came back full but nothing matched our URLs: that is a
    field-mapping misconfiguration, and writing would silently route every
    contact down the no-profile template path. Leave the run claimed."""
    client = FakeClient()
    client.runs["r1"] = RunInfo("r1", "SUCCEEDED", "ds1")
    client.items["ds1"] = [{"someOtherField": "https://linkedin.com/in/a"}]
    state["pending"] = ["r1"]
    state["run_contacts"]["r1"] = targets("https://linkedin.com/in/a")

    stats = await scraper.run(client)

    assert state["written"] == []
    assert state["released_runs"] == []
    assert stats.errors


@pytest.mark.asyncio
async def test_empty_dataset_marks_all_scrape_failed(state):
    """An empty dataset from a SUCCEEDED run is a real outcome (private or
    deleted profiles), distinct from the mapping-failure guard above."""
    client = FakeClient()
    client.runs["r1"] = RunInfo("r1", "SUCCEEDED", "ds1")
    client.items["ds1"] = []
    state["pending"] = ["r1"]
    state["run_contacts"]["r1"] = targets("https://linkedin.com/in/a")

    stats = await scraper.run(client)

    (written,) = state["written"]
    assert written[0]["profile"] is None
    assert stats.scrape_failed == 1


# ---------------------------------------------------------------------
# starter
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_starter_launches_runs_up_to_cap(state, monkeypatch):
    monkeypatch.setattr(config, "APIFY_MAX_ACTIVE_RUNS", 2)
    monkeypatch.setattr(config, "SCRAPE_BATCH_SIZE", 2)
    client = FakeClient()
    state["claims"] = [
        targets("https://linkedin.com/in/a", "https://linkedin.com/in/b"),
        targets("https://linkedin.com/in/c", "https://linkedin.com/in/d"),
        targets("https://linkedin.com/in/e", "https://linkedin.com/in/f"),
    ]

    stats = await scraper.run(client)

    assert stats.runs_started == 2  # third batch waits for the next trigger
    assert stats.contacts_claimed == 4
    assert len(state["run_ids_set"]) == 2
    assert state["claims"]  # the un-started batch was never claimed


@pytest.mark.asyncio
async def test_starter_stops_at_short_batch(state, monkeypatch):
    monkeypatch.setattr(config, "APIFY_MAX_ACTIVE_RUNS", 5)
    monkeypatch.setattr(config, "SCRAPE_BATCH_SIZE", 10)
    client = FakeClient()
    state["claims"] = [targets("https://linkedin.com/in/a")]

    stats = await scraper.run(client)

    assert stats.runs_started == 1
    assert client.started == [["https://linkedin.com/in/a"]]


@pytest.mark.asyncio
async def test_start_failure_releases_claims_and_aborts(state, monkeypatch):
    """Vendor down while starting: release the claimed batch back to
    'enriched' and stop — starting more runs would fail identically."""
    monkeypatch.setattr(config, "APIFY_MAX_ACTIVE_RUNS", 5)
    monkeypatch.setattr(config, "SCRAPE_BATCH_SIZE", 1)
    client = FakeClient()
    client.start_error = ProviderError("apify: gave up")
    state["claims"] = [
        targets("https://linkedin.com/in/a"),
        targets("https://linkedin.com/in/b"),
    ]

    stats = await scraper.run(client)

    assert state["released_ids"] == [[1]]
    assert stats.runs_started == 0
    assert stats.released == 1
    assert state["claims"]  # second batch untouched


@pytest.mark.asyncio
async def test_stale_claims_recovered(state):
    state["stale"] = 3
    stats = await scraper.run(FakeClient())
    assert stats.stale_recovered == 3


# ---------------------------------------------------------------------
# smoke: the whole package imports (guards against the transfer damage
# that left repo.py with a mid-file __future__ import)
# ---------------------------------------------------------------------


def test_package_imports():
    import app.api  # noqa: F401
    import app.runner  # noqa: F401
    import app.providers.apollo  # noqa: F401

    assert isinstance(ApifyClient(token="t", actor_id="user/actor").actor_id, str)
    assert ApifyClient(token="t", actor_id="user/actor").actor_id == "user~actor"
