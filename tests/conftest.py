"""Shared test scaffolding.

Two autouse guards enforce what the suite already promises informally —
"fakes only, no database or vendor calls" — so a test cannot pass (or
fail) for reasons that live outside the repository:

- ``_pinned_config``: app/config.py calls load_dotenv() at import, so
  every test would otherwise read the developer's real .env. That is not
  hypothetical: a live .env here sets APIFY_URL_FIELDS to one field where
  the scraper tests assume five, APIFY_INPUT_KEY to 'queries', and
  DRAFT_PROVIDER to 'n8n'. Pinning the documented defaults makes the
  suite a property of the code alone. test_scraper.py's local pin of
  APIFY_URL_FIELDS predates this and still works — a test's own
  monkeypatch always wins over the class-level pin here.

- ``_no_database``: repo.py does ``from .db import pool``, so faking
  app.db.pool would miss it. Patching repo.pool turns "this test forgot
  to fake a repo function" from a confusing "Pool not initialised" into
  a message naming the rule that was broken.

Nothing here is required to write a test. ``patch_repo`` and
``no_backoff`` are opt-in conveniences for the boilerplate that every
existing module hand-rolls.
"""
from __future__ import annotations

import asyncio

import pytest

from app import db, repo
from app.config import config

# The values a fresh checkout with no .env would see (app/config.py's
# own defaults), plus deterministic stand-ins where an empty string
# would disable the code path under test rather than neutralise it.
# Anything env-derived that a test could assert on belongs here.
_CONFIG_DEFAULTS = {
    # core queue
    "BATCH_SIZE": 100,
    "MAX_PASSES": 20,
    "STALE_CLAIM_MINUTES": 15,
    "CACHE_MAX_AGE_DAYS": 365,
    "CACHE_MIN_CONFIDENCE": 0.70,
    "PROVIDER": "apollo",
    # apollo
    "APOLLO_API_KEY": "test-apollo-key",
    "APOLLO_BATCH_SIZE": 10,
    "APOLLO_CHUNK_DELAY_SECONDS": 0.0,   # no real pacing sleeps in tests
    "MIN_ACCEPT_CONFIDENCE": 0.55,
    "PROVIDER_TIMEOUT_SECONDS": 60,
    "PROVIDER_MAX_RETRIES": 4,
    # apify
    "APIFY_TOKEN": "test-apify-token",
    "APIFY_ACTOR_ID": "test/actor",
    "APIFY_INPUT_KEY": "profileUrls",
    "APIFY_EXTRA_INPUT": "",
    "APIFY_URL_FIELDS": ("url", "profileUrl", "linkedinUrl", "inputUrl", "publicUrl"),
    "SCRAPE_BATCH_SIZE": 50,
    "APIFY_MAX_ACTIVE_RUNS": 3,
    # drafting
    "DRAFT_PROVIDER": "groq",
    "N8N_LLM_URL": "",
    "GROQ_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "DRAFT_MODEL": "",
    "DRAFT_EFFORT": "high",
    "DRAFT_MAX_TOKENS": 4000,
    "DRAFT_BATCH_SIZE": 25,
    "DRAFT_PROFILE_CHAR_LIMIT": 6000,
    # verification
    "VERIFY_ENABLED": True,
    "VERIFY_BATCH_SIZE": 25,
    # email
    "MAILJET_API_KEY": "",
    "MAILJET_SECRET_KEY": "",
    "SMARTLEAD_API_KEY": "",
    "RESEND_API_KEY": "",
    "SEND_BATCH_SIZE": 25,
    "SMARTLEAD_SCHEDULE_TIMEZONE": "America/New_York",
    "SMARTLEAD_SCHEDULE_DAYS": (1, 2, 3, 4, 5),
    "SMARTLEAD_SCHEDULE_START_HOUR": "09:00",
    "SMARTLEAD_SCHEDULE_END_HOUR": "17:00",
    "SMARTLEAD_MAX_NEW_LEADS_PER_DAY": 20,
    "SMARTLEAD_MIN_TIME_BTW_EMAILS": 10,
    # scheduler — off, so no test can start a real background job
    "SCHEDULER_ENABLED": False,
    "SCHEDULER_INTERVAL_MINUTES": 5,
    "SCHEDULER_STAGES": ("enrich", "scrape", "verify", "draft", "email"),
    # ui / api
    "SUPABASE_URL": "",
    "SUPABASE_ANON_KEY": "",
    "SESSION_SECRET": "",
    "SESSION_MAX_AGE_MINUTES": 480,
    "N8N_INGEST_URL": "",
    "CSV_MAX_BYTES": 2_000_000,
    "API_KEY": "",
    "DATABASE_URL": "",
}


@pytest.fixture(autouse=True)
def _pinned_config(monkeypatch):
    """Detach every test from the developer's .env.

    Set on the class, not the instance: Config's checks are classmethods
    reading ``cls.X``, so a class attribute is what the real code sees.
    A test that patches either the class or the instance still overrides
    this, which is why the existing modules' own pins keep working.

    The delattr pass first is load-bearing. ``monkeypatch.setattr(config,
    "X", ...)`` — patching the *instance*, which most test modules here do
    — saves the value inherited from the class and restores it by
    ASSIGNING it onto the instance. That leaves a permanent instance
    attribute behind, which then shadows the class pin below for every
    later test in the session. Clearing the instance __dict__ each time
    makes the class the single source of truth again; monkeypatch puts
    back whatever it removed at teardown.
    """
    for name in list(vars(config)):
        monkeypatch.delattr(config, name, raising=False)
    for name, value in _CONFIG_DEFAULTS.items():
        monkeypatch.setattr(type(config), name, value, raising=False)


@pytest.fixture(autouse=True)
def _no_database(monkeypatch):
    """Fail loudly, and with the reason, if a test reaches for Postgres."""

    def forbidden():
        raise AssertionError(
            "this test called a real repo function — the suite is fakes-only. "
            "Replace the repo function with a fake "
            "(see patch_repo, or any tests/test_*.py 'state' fixture)."
        )

    monkeypatch.setattr(repo, "pool", forbidden)
    assert db._pool is None, "a previous test left a live connection pool open"


@pytest.fixture
def patch_repo(monkeypatch):
    """Swap repo functions for fakes, keyed by the real function's name.

        async def claim_batch(limit=None): return []
        patch_repo(claim_batch)

    Equivalent to the ``for fn in (...): monkeypatch.setattr(...)`` loop
    the existing modules write by hand; both styles are fine.
    """

    def apply(*fns, **named):
        for fn in fns:
            if not hasattr(repo, fn.__name__):
                raise AttributeError(f"repo has no function named {fn.__name__!r}")
            monkeypatch.setattr(repo, fn.__name__, fn)
        for name, fn in named.items():
            if not hasattr(repo, name):
                raise AttributeError(f"repo has no function named {name!r}")
            monkeypatch.setattr(repo, name, fn)

    return apply


@pytest.fixture
def no_backoff(monkeypatch):
    """Collapse retry backoff to nothing and record what was requested.

    The provider retry loops sleep up to 30s between attempts, which
    would make an honest exhaustion test unrunnable. Yields the list of
    requested delays so a test can assert the backoff schedule itself
    rather than just that it finished quickly.
    """
    real_sleep = asyncio.sleep
    delays: list[float] = []

    async def instant(seconds, *args, **kwargs):
        delays.append(seconds)
        # Still yield to the loop, so ordering between concurrent tasks
        # behaves as it does in production.
        return await real_sleep(0, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", instant)
    return delays
