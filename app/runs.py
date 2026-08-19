"""Per-stage run guards and background execution.

Shared by the JSON API and the UI so neither imports the other and the
UI never loops HTTP back to its own process. One run per stage at a
time: check-and-set has no await between them, so each guard is atomic
under asyncio's cooperative scheduling.
"""
from __future__ import annotations

import asyncio
import logging

from . import drafting, emailer, scraper, verifier
from . import runner as enrichment
from .config import config

log = logging.getLogger(__name__)

STAGES = ("enrich", "scrape", "verify", "draft", "email")

# Completion nudges: when a stage finishes, poke the stage its output
# feeds so work flows tick-free. This is NOT stage coupling — the next
# stage still claims its own work from the queue; the nudge only starts
# a run that would otherwise wait for the scheduler. Deliberately no
# draft→email link: the human review gate is the pipeline's whole point.
_NEXT = {"enrich": "scrape", "scrape": "verify", "verify": "draft"}

_running: dict[str, bool] = {stage: False for stage in STAGES}
# Strong references: asyncio only keeps weak refs to tasks, and a
# garbage-collected run would vanish silently mid-flight.
_tasks: set[asyncio.Task] = set()


async def _run_stage(stage: str) -> None:
    try:
        if stage == "enrich":
            stats = await enrichment.run(enrichment.build_provider())
        elif stage == "scrape":
            stats = await scraper.run()
        elif stage == "verify":
            stats = await verifier.run()
        elif stage == "draft":
            stats = await drafting.run()
        else:
            stats = await emailer.run()
        log.info("%s run finished: %s", stage, stats.as_dict())
    except Exception:
        log.exception("%s run crashed", stage)
    finally:
        _running[stage] = False
    # After the flag clears, so the chained stage's guard sees reality.
    # Chained even after a crash: a partial run may still have advanced
    # contacts the next stage can use, and an empty run is one cheap
    # no-op query.
    _nudge_next(stage)


def _nudge_next(stage: str) -> None:
    """Nudge the next runnable stage in the chain, walking PAST any stage
    that is unconfigured/disabled — so turning verify off doesn't strand
    draft (scrape's completion then nudges draft directly)."""
    nxt = _NEXT.get(stage)
    while nxt is not None:
        if missing_config(nxt):
            nxt = _NEXT.get(nxt)
            continue
        nudge(nxt)
        return


def missing_config(stage: str) -> list[str]:
    """Env vars this stage needs beyond the startup-validated core."""
    if stage == "scrape":
        return config.missing_scrape_vars()
    if stage == "verify":
        return config.missing_verify_vars()
    if stage == "draft":
        return config.missing_draft_vars()
    if stage == "email":
        return config.missing_email_vars()
    return []


def try_start(stage: str) -> bool:
    """Start a background run; False if one is already in progress."""
    if stage not in _running:
        raise ValueError(f"unknown stage: {stage!r}")
    if _running[stage]:
        return False
    _running[stage] = True
    task = asyncio.get_running_loop().create_task(_run_stage(stage))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


def nudge(stage: str) -> bool:
    """Best-effort start: skip silently when the stage is unconfigured,
    already running, or anything else objects. Callers (ingest, approve,
    stage completion) fire-and-forget — the scheduler is the safety net
    for every nudge that doesn't land."""
    try:
        if missing_config(stage):
            return False
        return try_start(stage)
    except Exception:
        log.exception("nudge(%s) failed", stage)
        return False


def send_now(campaign_id: int) -> bool:
    """Fire-and-forget immediate drain of ONE campaign's approved queue — the
    'immediate' send mode's trigger on approval (emailer.send_campaign_now,
    which bypasses the business-hours window + drip pacing).

    Deliberately NOT stage-guarded like nudge(): the claim's FOR UPDATE SKIP
    LOCKED makes a drain safe to run alongside the drip and other drains (no
    double-send), so a second approval need not wait on the first. It shares
    the email config gate (no keys -> no-op) and the strong-task-ref set so a
    run isn't garbage-collected mid-flight."""
    try:
        if missing_config("email"):
            return False
        task = asyncio.get_running_loop().create_task(_send_now(campaign_id))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return True
    except Exception:
        log.exception("send_now(%d) failed", campaign_id)
        return False


async def _send_now(campaign_id: int) -> None:
    try:
        stats = await emailer.send_campaign_now(campaign_id)
        log.info("immediate send for campaign %d finished: %s",
                 campaign_id, stats.as_dict())
    except Exception:
        log.exception("immediate send for campaign %d crashed", campaign_id)


def status() -> dict[str, bool]:
    return dict(_running)
