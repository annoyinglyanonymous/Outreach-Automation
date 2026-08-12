"""Run-guard semantics: one run per stage, and a crash must clear the
flag or the stage becomes untriggerable until restart."""
from __future__ import annotations

import asyncio

import pytest

from app import runs
from app.config import config


class _Stats:
    def as_dict(self) -> dict:
        return {}


@pytest.fixture(autouse=True)
def reset_flags(monkeypatch):
    # Chaining off by default so guard tests don't cascade into real
    # runners; the chaining tests re-enable _NEXT explicitly.
    monkeypatch.setattr(runs, "_NEXT", {})
    for stage in runs.STAGES:
        runs._running[stage] = False
    yield
    for stage in runs.STAGES:
        runs._running[stage] = False


@pytest.mark.asyncio
async def test_second_start_refused_then_allowed_after_finish(monkeypatch):
    release = asyncio.Event()

    async def slow_run(*args, **kwargs):
        await release.wait()
        return _Stats()

    monkeypatch.setattr(runs.scraper, "run", slow_run)

    assert runs.try_start("scrape") is True
    assert runs.try_start("scrape") is False
    assert runs.status()["scrape"] is True

    release.set()
    await asyncio.gather(*runs._tasks)

    assert runs.status()["scrape"] is False
    assert runs.try_start("scrape") is True
    await asyncio.gather(*runs._tasks)


@pytest.mark.asyncio
async def test_crash_clears_the_flag(monkeypatch):
    async def crash(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(runs.drafting, "run", crash)

    assert runs.try_start("draft") is True
    await asyncio.gather(*runs._tasks, return_exceptions=True)
    assert runs.status()["draft"] is False


@pytest.mark.asyncio
async def test_enrich_uses_the_configured_provider(monkeypatch):
    seen = {}

    def build_provider():
        return "provider-sentinel"

    async def run(provider):
        seen["provider"] = provider
        return _Stats()

    monkeypatch.setattr(runs.enrichment, "build_provider", build_provider)
    monkeypatch.setattr(runs.enrichment, "run", run)

    assert runs.try_start("enrich") is True
    await asyncio.gather(*runs._tasks)
    assert seen["provider"] == "provider-sentinel"


def test_unknown_stage_raises():
    with pytest.raises(ValueError):
        runs.try_start("send")


# ---------------------------------------------------------------------
# completion chaining / nudge
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_completion_chains_to_next(monkeypatch):
    """enrich finishing must start scrape without a scheduler tick."""
    monkeypatch.setattr(runs, "_NEXT", {"enrich": "scrape"})
    monkeypatch.setattr(runs, "missing_config", lambda stage: [])
    started = []

    async def enrich_run(provider):
        return _Stats()

    async def scrape_run():
        started.append("scrape")
        return _Stats()

    monkeypatch.setattr(runs.enrichment, "build_provider", lambda: None)
    monkeypatch.setattr(runs.enrichment, "run", enrich_run)
    monkeypatch.setattr(runs.scraper, "run", scrape_run)

    assert runs.try_start("enrich") is True
    # Two rounds: the first gather finishes enrich (which spawns the
    # scrape task), the second finishes scrape.
    await asyncio.gather(*runs._tasks)
    await asyncio.gather(*runs._tasks)
    assert started == ["scrape"]


@pytest.mark.asyncio
async def test_chain_fires_even_after_a_crash(monkeypatch):
    """A partial run may still have advanced contacts downstream."""
    monkeypatch.setattr(runs, "_NEXT", {"enrich": "scrape"})
    monkeypatch.setattr(runs, "missing_config", lambda stage: [])
    started = []

    async def crash(provider):
        raise RuntimeError("boom")

    async def scrape_run():
        started.append("scrape")
        return _Stats()

    monkeypatch.setattr(runs.enrichment, "build_provider", lambda: None)
    monkeypatch.setattr(runs.enrichment, "run", crash)
    monkeypatch.setattr(runs.scraper, "run", scrape_run)

    runs.try_start("enrich")
    await asyncio.gather(*runs._tasks)
    await asyncio.gather(*runs._tasks)
    assert started == ["scrape"]


@pytest.mark.asyncio
async def test_nudge_skips_unconfigured_stage(monkeypatch):
    monkeypatch.setattr(runs, "missing_config", lambda stage: ["SOME_KEY"])
    assert runs.nudge("email") is False
    assert runs.status()["email"] is False


@pytest.mark.asyncio
async def test_nudge_skips_running_stage_and_never_raises(monkeypatch):
    monkeypatch.setattr(runs, "missing_config", lambda stage: [])
    runs._running["email"] = True
    assert runs.nudge("email") is False

    def explode(stage):
        raise RuntimeError("boom")

    monkeypatch.setattr(runs, "missing_config", explode)
    assert runs.nudge("email") is False  # swallowed, logged


def test_missing_config_maps_stages(monkeypatch):
    # missing_*_vars are classmethods, so the class attribute is what counts.
    monkeypatch.setattr(type(config), "APIFY_TOKEN", "")
    monkeypatch.setattr(type(config), "APIFY_ACTOR_ID", "x")
    monkeypatch.setattr(type(config), "GROQ_API_KEY", "")
    monkeypatch.setattr(type(config), "ANTHROPIC_API_KEY", "")
    assert runs.missing_config("enrich") == []
    assert runs.missing_config("scrape") == ["APIFY_TOKEN"]

    # The draft check follows the configured provider.
    monkeypatch.setattr(type(config), "DRAFT_PROVIDER", "groq")
    assert runs.missing_config("draft") == ["GROQ_API_KEY"]
    monkeypatch.setattr(type(config), "DRAFT_PROVIDER", "anthropic")
    assert runs.missing_config("draft") == ["ANTHROPIC_API_KEY"]
