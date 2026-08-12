"""In-process scheduler: triggers the queue-driven stages on an interval
so the pipeline runs unattended.

It calls the SAME guarded entrypoint the API and UI use
(``runs.try_start``), so a scheduled tick can never overlap a manual run
or a previous tick — an in-progress stage is simply skipped and picked up
next interval. Nothing here talks to the database or a vendor; it only
pokes the run guards.

Deliberately it does NOT touch review: approval stays a human step, so the
email stage only ever sends drafts a person approved. The scheduler just
keeps enrich/scrape/draft/email polling their queues.

Off by default (``SCHEDULER_ENABLED``). Because every stage is idempotent
and eventually-consistent — enrich feeds scrape feeds draft feeds email —
firing them all on the same interval is fine: whatever a stage isn't ready
for yet, it drains on a later tick.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import runs
from .config import config

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def scheduled_stages() -> list[str]:
    """Configured stages, filtered to real ones — a typo in
    SCHEDULER_STAGES drops that entry instead of erroring the run."""
    return [s for s in config.SCHEDULER_STAGES if s in runs.STAGES]


# Async so APScheduler runs it on the event loop rather than a worker
# thread — try_start calls get_running_loop(), which needs the loop.
async def _tick(stage: str) -> None:
    # Mirror the API/UI guard: never start a stage whose config is
    # incomplete (it would only 503 or no-op), and let try_start skip a
    # stage that is already running.
    missing = runs.missing_config(stage)
    if missing:
        log.debug("scheduler: skipping %s — missing config: %s", stage, missing)
        return
    if runs.try_start(stage):
        log.info("scheduler: started %s", stage)
    else:
        log.debug("scheduler: %s already running, skipped", stage)


def start() -> None:
    """Start the interval scheduler if enabled. Idempotent — a second
    call while running is a no-op, so an accidental double-invoke on
    startup cannot double-schedule."""
    global _scheduler
    if not config.SCHEDULER_ENABLED:
        log.info("scheduler disabled — set SCHEDULER_ENABLED=true to automate the pipeline")
        return
    if _scheduler is not None:
        return

    stages = scheduled_stages()
    if not stages:
        log.warning("scheduler enabled but SCHEDULER_STAGES has no valid stage; not starting")
        return

    interval = config.SCHEDULER_INTERVAL_MINUTES
    scheduler = AsyncIOScheduler()
    for stage in stages:
        scheduler.add_job(
            _tick,
            "interval",
            args=[stage],
            minutes=interval,
            # Spread the stages so they don't all fire on the same tick.
            jitter=min(30, max(1, interval * 60 // 4)),
            id=f"stage:{stage}",
            # If the loop was busy and a fire was missed, run once when
            # free rather than replaying every skipped interval.
            coalesce=True,
            max_instances=1,
        )
    scheduler.start()
    _scheduler = scheduler
    log.info("scheduler started: stages=%s every %d min", stages, interval)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        log.info("scheduler stopped")


def info() -> dict:
    """Surfaced in /stats and the dashboard so the automation state is
    visible rather than guessed."""
    return {
        "enabled": config.SCHEDULER_ENABLED,
        "running": _scheduler is not None,
        "interval_minutes": config.SCHEDULER_INTERVAL_MINUTES,
        "stages": scheduled_stages(),
    }
