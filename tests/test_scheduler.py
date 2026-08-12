"""Scheduler tests — no real timers. They lock in the guard logic a tick
relies on: skip a stage with missing config, skip one already running,
start one that is ready, and never touch a stage the config excludes."""
from __future__ import annotations

import pytest

from app import runs, scheduler
from app.config import config


@pytest.fixture
def spy(monkeypatch):
    """Record try_start calls and control what runs/missing_config say."""
    state = {"started": [], "running": set(), "missing": {}}

    def try_start(stage):
        if stage in state["running"]:
            return False
        state["started"].append(stage)
        state["running"].add(stage)
        return True

    def missing_config(stage):
        return state["missing"].get(stage, [])

    monkeypatch.setattr(runs, "try_start", try_start)
    monkeypatch.setattr(runs, "missing_config", missing_config)
    return state


@pytest.mark.asyncio
async def test_tick_starts_a_ready_stage(spy):
    await scheduler._tick("enrich")
    assert spy["started"] == ["enrich"]


@pytest.mark.asyncio
async def test_tick_skips_stage_with_missing_config(spy):
    spy["missing"]["email"] = ["SMARTLEAD_API_KEY or RESEND_API_KEY"]
    await scheduler._tick("email")
    assert spy["started"] == []


@pytest.mark.asyncio
async def test_tick_skips_stage_already_running(spy):
    spy["running"].add("draft")
    await scheduler._tick("draft")
    assert spy["started"] == []


def test_scheduled_stages_filters_unknown(monkeypatch):
    monkeypatch.setattr(config, "SCHEDULER_STAGES", ("enrich", "bogus", "email"))
    assert scheduler.scheduled_stages() == ["enrich", "email"]


def test_start_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    scheduler._scheduler = None
    scheduler.start()
    assert scheduler._scheduler is None
    assert scheduler.info()["running"] is False


@pytest.mark.asyncio
async def test_start_and_shutdown_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", True)
    monkeypatch.setattr(config, "SCHEDULER_STAGES", ("enrich", "draft"))
    monkeypatch.setattr(config, "SCHEDULER_INTERVAL_MINUTES", 5)
    scheduler._scheduler = None
    try:
        scheduler.start()
        assert scheduler._scheduler is not None
        # One job per configured stage, no more.
        job_ids = {j.id for j in scheduler._scheduler.get_jobs()}
        assert job_ids == {"stage:enrich", "stage:draft"}
        info = scheduler.info()
        assert info["running"] is True
        assert info["stages"] == ["enrich", "draft"]
    finally:
        scheduler.shutdown()
    assert scheduler._scheduler is None


@pytest.mark.asyncio
async def test_start_is_idempotent(monkeypatch):
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", True)
    monkeypatch.setattr(config, "SCHEDULER_STAGES", ("enrich",))
    scheduler._scheduler = None
    try:
        scheduler.start()
        first = scheduler._scheduler
        scheduler.start()  # second call must not replace or double-schedule
        assert scheduler._scheduler is first
        assert len(scheduler._scheduler.get_jobs()) == 1
    finally:
        scheduler.shutdown()


def test_start_skips_when_no_valid_stage(monkeypatch):
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", True)
    monkeypatch.setattr(config, "SCHEDULER_STAGES", ("bogus",))
    scheduler._scheduler = None
    scheduler.start()
    assert scheduler._scheduler is None
