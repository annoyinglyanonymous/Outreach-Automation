from __future__ import annotations
import logging
import time
from dataclasses import dataclass, field

from . import repo
from .config import config
from .providers.base import EnrichmentResult, Provider, ProviderError

log = logging.getLogger(__name__)


@dataclass
class RunStats:
    passes: int = 0
    claimed: int = 0
    from_cache: int = 0
    from_provider: int = 0
    unresolved: int = 0
    released: int = 0
    stale_recovered: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "passes": self.passes,
            "claimed": self.claimed,
            "from_cache": self.from_cache,
            "from_provider": self.from_provider,
            "unresolved": self.unresolved,
            "released": self.released,
            "stale_recovered": self.stale_recovered,
            "seconds": round(self.seconds, 1),
            "errors": self.errors,
        }


async def _enrich_one_batch(
    contacts: list[repo.Contact],
    provider: Provider,
    stats: RunStats,
) -> bool:
    cached = await repo.cache_lookup([c.email for c in contacts])

    results: list[dict] = []
    misses: list[repo.Contact] = []

    for contact in contacts:
        hit = cached.get(contact.email)
        if hit:
            url, confidence = hit
            results.append({
                "id": contact.id,
                "linkedin_url": url,
                "linkedin_confidence": confidence,
                "tier": "cache",
            })
            stats.from_cache += 1
        else:
            misses.append(contact)

    # -- tier 1: paid lookup, misses only --------------------------------
    if misses:
        try:
            found: list[EnrichmentResult] = await provider.enrich(misses)
        except ProviderError as exc:
            log.error("provider failed, releasing %d contacts: %s", len(misses), exc)
            stats.errors.append(str(exc))
            stats.released += await repo.release_claims([c.id for c in misses])
            if results:
                await repo.write_results(results)
            return False

        by_email = {r.email: r for r in found}
        for contact in misses:
            result = by_email.get(contact.email)
            url = result.linkedin_url if result else None
            results.append({
                "id": contact.id,
                "linkedin_url": url,
                "linkedin_confidence": result.confidence if result else 0.0,
                "tier": provider.name,
            })
            if url:
                stats.from_provider += 1
            else:
                stats.unresolved += 1

    await repo.write_results(results)
    return True


async def run(provider: Provider, max_passes: int | None = None) -> RunStats:
    started = time.monotonic()
    stats = RunStats()
    limit = max_passes or config.MAX_PASSES

    # Recover rows a crashed run left marked in-progress. They are
    # invisible to the queue and nothing errored, so without this they
    # would sit stranded forever.
    stats.stale_recovered = await repo.reset_stale_claims()
    if stats.stale_recovered:
        log.info("recovered %d stale claims", stats.stale_recovered)

    while stats.passes < limit:
        contacts = await repo.claim_batch()
        if not contacts:
            break

        stats.passes += 1
        stats.claimed += len(contacts)
        log.info("pass %d: claimed %d", stats.passes, len(contacts))

        if not await _enrich_one_batch(contacts, provider, stats):
            break

        # A short batch means the queue is drained; anything else and
        # there is probably more waiting.
        if len(contacts) < config.BATCH_SIZE:
            break

    stats.seconds = time.monotonic() - started
    log.info("run complete: %s", stats.as_dict())
    return stats


def build_provider() -> Provider:
    """Instantiate the configured tier-1 provider.

    Swapping vendors is a config change, not a code change: the runner
    only ever sees the Provider protocol.
    """
    if config.PROVIDER == "apollo":
        from .providers.apollo import ApolloProvider
        return ApolloProvider()
    raise RuntimeError(f"Unknown PROVIDER: {config.PROVIDER!r}")