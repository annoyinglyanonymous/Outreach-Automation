"""Stage 2 runner: profile scraping via Apify.

Shaped like runner.py but split in two because Apify is asynchronous:
the collector reconciles runs that have finished since the last trigger,
then the starter claims new batches and launches runs, up to a cap on
concurrent runs. Each trigger does both, so a single scheduled ping
drains the queue over successive invocations without anything blocking.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from . import repo
from .config import config
from .providers.apify import ApifyClient
from .providers.base import ProviderError

log = logging.getLogger(__name__)


@dataclass
class ScrapeStats:
    stale_recovered: int = 0
    runs_checked: int = 0
    runs_collected: int = 0
    runs_failed: int = 0
    runs_started: int = 0
    contacts_claimed: int = 0
    profiles_written: int = 0
    scrape_failed: int = 0
    released: int = 0
    in_flight: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "stale_recovered": self.stale_recovered,
            "runs_checked": self.runs_checked,
            "runs_collected": self.runs_collected,
            "runs_failed": self.runs_failed,
            "runs_started": self.runs_started,
            "contacts_claimed": self.contacts_claimed,
            "profiles_written": self.profiles_written,
            "scrape_failed": self.scrape_failed,
            "released": self.released,
            "in_flight": self.in_flight,
            "seconds": round(self.seconds, 1),
            "errors": self.errors,
        }


def canonical_url(url: str | None) -> str:
    """Reduce a LinkedIn URL to a comparable identity.

    Apollo, the actor and the source list can disagree on scheme, www,
    country subdomain, trailing slash and tracking params for the same
    profile. The /in/<slug> path is the identity whenever present.
    """
    if not url:
        return ""
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    m = re.search(r"linkedin\.com(/in/[^/?#]+)", u)
    if m:
        return m.group(1).rstrip("/")
    u = re.sub(r"^www\.", "", u)
    return u.split("?", 1)[0].split("#", 1)[0].rstrip("/")


def item_url(item: dict) -> str:
    for key in config.APIFY_URL_FIELDS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


async def _collect_run(client: ApifyClient, run_id: str, stats: ScrapeStats) -> bool:
    """Reconcile one run. Returns True while it still occupies a run slot."""
    try:
        info = await client.get_run(run_id)
    except ProviderError as exc:
        # Apify unreachable. The run (and its dataset) will still exist
        # next trigger; the contacts stay claimed and nothing is lost.
        log.error("run %s: status check failed: %s", run_id, exc)
        stats.errors.append(str(exc))
        return True

    if info.in_flight:
        return True

    if not info.succeeded:
        released = await repo.release_run(run_id)
        stats.runs_failed += 1
        stats.released += released
        log.warning("run %s ended %s, released %d contacts for re-scrape",
                    run_id, info.status, released)
        return False

    if not info.dataset_id:
        stats.errors.append(f"run {run_id}: succeeded but has no dataset id")
        log.error("run %s: succeeded but has no dataset id", run_id)
        return True

    try:
        items = await client.fetch_items(info.dataset_id)
    except ProviderError as exc:
        # Same as the status check: the dataset persists, retry is free.
        log.error("run %s: dataset fetch failed: %s", run_id, exc)
        stats.errors.append(str(exc))
        return True

    contacts = await repo.run_contacts(run_id)
    if not contacts:
        # Already collected by a previous pass that crashed after writing.
        return False

    by_url = {}
    for item in items:
        key = canonical_url(item_url(item))
        if key:
            by_url[key] = item

    results = []
    matched = 0
    for contact in contacts:
        profile = by_url.get(canonical_url(contact.linkedin_url))
        if profile is not None:
            matched += 1
        results.append({"id": contact.id, "profile": profile, "run_id": run_id})

    if items and matched == 0:
        # The actor returned data but none of it matched our URLs — almost
        # certainly APIFY_URL_FIELDS not matching this actor's output, not
        # 50 simultaneously vanished profiles. Writing would silently send
        # every contact down the no-profile template path. Leave the run
        # claimed: the dataset persists, so re-collecting after the config
        # fix costs nothing.
        message = (
            f"run {run_id}: {len(items)} dataset items but none matched a "
            f"contact URL — check APIFY_URL_FIELDS against the actor's output"
        )
        log.error(message)
        stats.errors.append(message)
        return True

    await repo.write_profiles(results)
    stats.runs_collected += 1
    stats.profiles_written += matched
    stats.scrape_failed += len(results) - matched
    log.info("run %s collected: %d profiles, %d not in dataset",
             run_id, matched, len(results) - matched)
    return False


async def _start_runs(client: ApifyClient, stats: ScrapeStats, active: int) -> None:
    while active < config.APIFY_MAX_ACTIVE_RUNS:
        batch = await repo.claim_scrape_batch()
        if not batch:
            break

        ids = [c.id for c in batch]
        try:
            run_id = await client.start_run([c.linkedin_url for c in batch])
        except ProviderError as exc:
            # Vendor down or misconfigured: starting more runs would fail
            # identically, so release and stop — same abort rationale as
            # the enrichment runner.
            log.error("start_run failed, releasing %d contacts: %s", len(ids), exc)
            stats.errors.append(str(exc))
            stats.released += await repo.release_scrape_claims(ids)
            break

        # A crash between start_run and here strands the rows at 'scraping'
        # with no run id; reset_stale_scrape_claims recovers them and the
        # orphaned run wastes one batch of credit but corrupts nothing.
        await repo.set_run_id(run_id, ids)
        active += 1
        stats.runs_started += 1
        stats.contacts_claimed += len(batch)
        log.info("started run %s with %d profiles", run_id, len(batch))

        if len(batch) < config.SCRAPE_BATCH_SIZE:
            break


async def run(client: ApifyClient | None = None) -> ScrapeStats:
    client = client or ApifyClient()
    started = time.monotonic()
    stats = ScrapeStats()

    stats.stale_recovered = await repo.reset_stale_scrape_claims()
    if stats.stale_recovered:
        log.info("recovered %d stale scrape claims", stats.stale_recovered)

    # Collect first: finished runs free slots for the starter below.
    active = 0
    for run_id in await repo.pending_runs():
        stats.runs_checked += 1
        if await _collect_run(client, run_id, stats):
            active += 1

    await _start_runs(client, stats, active)

    stats.in_flight = active + stats.runs_started
    stats.seconds = time.monotonic() - started
    log.info("scrape run complete: %s", stats.as_dict())
    return stats
