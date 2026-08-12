"""Verify stage runner: AI enrichment verification, between scrape and draft.

For each contact that has a scraped profile and no verdict yet, one LLM
call judges whether the matched LinkedIn profile is really that person.
The verdict is applied through the SAME functions the manual verify page
uses:

- right_person                 -> confirm (keeps the personalised path)
- wrong_person / unsure        -> reject  (wipe the match; the contact
                                  falls back to the template/email-only draft)

Conservative on purpose. Runs only when an LLM provider is configured
(`config.missing_verify_vars`); otherwise `build_verifier` returns None
and the stage no-ops, leaving the human verify page as the path.

The queue drains without a status flip: a verdict is an event, and every
attempted contact is remembered in `seen` so the next batch excludes it —
so the loop always makes forward progress and terminates.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from . import repo, verification
from .config import config
from .providers.base import ProviderError

log = logging.getLogger(__name__)

AI_REVIEWER = "ai"


@dataclass
class VerifyStats:
    passes: int = 0
    checked: int = 0
    confirmed: int = 0
    rejected: int = 0
    skipped: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "passes": self.passes,
            "checked": self.checked,
            "confirmed": self.confirmed,
            "rejected": self.rejected,
            "skipped": self.skipped,
            "seconds": round(self.seconds, 1),
            "errors": self.errors,
        }


async def run(verifier=None, max_passes: int | None = None) -> VerifyStats:
    verifier = verifier if verifier is not None else verification.build_verifier()
    started = time.monotonic()
    stats = VerifyStats()

    if verifier is None:
        log.info("verify: no LLM provider configured — skipping (manual verify stands)")
        stats.seconds = time.monotonic() - started
        return stats

    limit = max_passes or config.MAX_PASSES
    seen: list[int] = []  # attempted this run — guarantees forward progress

    while stats.passes < limit:
        targets = await repo.verify_queue_batch(seen)
        if not targets:
            break
        stats.passes += 1

        aborted = False
        for target in targets:
            seen.append(target.id)
            try:
                verdict = await verification.verify_match(target, verifier)
            except ProviderError as exc:
                # Vendor down: stop. Verdicts already applied stand
                # (confirm/reject are per-contact atomic); the rest are
                # retried next tick. Nothing half-applied.
                log.error("verify: provider failed, stopping run: %s", exc)
                stats.errors.append(str(exc))
                aborted = True
                break

            stats.checked += 1
            reason = verdict.as_reason()
            if verdict.is_match:
                ok = await repo.confirm_enrichment(target.id, AI_REVIEWER, reason)
            else:
                ok = await repo.reject_enrichment(target.id, AI_REVIEWER, reason)
            # A no-op (contact raced out of an eligible state) is neither
            # confirm nor reject — count it separately so passes still add up.
            if not ok:
                stats.skipped += 1
            elif verdict.is_match:
                stats.confirmed += 1
            else:
                stats.rejected += 1

        if aborted or len(targets) < config.VERIFY_BATCH_SIZE:
            break

    stats.seconds = time.monotonic() - started
    log.info("verify run complete: %s", stats.as_dict())
    return stats
