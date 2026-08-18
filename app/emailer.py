"""Stage 4 runner: email sending.

Shaped like the drafting runner but with money-grade failure semantics —
the business rule is at most ONE first-touch email per contact, ever:

- Results are written per contact, immediately after the vendor accepts,
  not batched: a crash window of milliseconds instead of a batch.
- Stale 'sending' claims are NEVER auto-reset. A crash after the vendor
  accepted but before our write leaves rows whose email went out;
  resetting them to 'drafted' would re-send. They are counted, surfaced
  in stats and the dashboard, and resolved by a human.
- Releasing an in-flight contact on vendor failure is safe only because
  the provider is idempotent per contact (Resend: Idempotency-Key;
  Smartlead: duplicate leads in a campaign are skipped). Mailjet has NO
  idempotency key, so its provider raises SendUncertain for ambiguous
  post-send failures — the contact is left at 'sending' (surfaced, never
  released) rather than risk a re-send.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from . import repo
from .config import config
from .providers.base import EmailSender, ProviderError, SendRejected, SendUncertain

log = logging.getLogger(__name__)


@dataclass
class EmailStats:
    passes: int = 0
    claimed: int = 0
    sent: int = 0
    rejected: int = 0
    suppressed: int = 0
    released: int = 0
    uncertain: int = 0
    stuck_sending: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "passes": self.passes,
            "claimed": self.claimed,
            "sent": self.sent,
            "rejected": self.rejected,
            "suppressed": self.suppressed,
            "released": self.released,
            "uncertain": self.uncertain,
            "stuck_sending": self.stuck_sending,
            "seconds": round(self.seconds, 1),
            "errors": self.errors,
        }


def build_senders() -> dict[str, EmailSender]:
    """Consent value -> sender, from whichever API keys are configured.
    The claim query only picks contacts whose consent has a sender here,
    so a missing key narrows the queue instead of failing the run."""
    senders: dict[str, EmailSender] = {}
    # Cold now sends via Mailjet; Smartlead is kept only as a fallback
    # during cutover (if the Mailjet pair isn't configured yet).
    if config.MAILJET_API_KEY and config.MAILJET_SECRET_KEY:
        from .providers.mailjet import MailjetSender
        senders["cold"] = MailjetSender()
    elif config.SMARTLEAD_API_KEY:
        from .providers.smartlead import SmartleadSender
        senders["cold"] = SmartleadSender()
    if config.RESEND_API_KEY:
        from .providers.resend import ResendSender
        senders["opted_in"] = ResendSender()
    return senders


async def _send_batch(
    targets: list["repo.EmailTarget"],
    senders: dict[str, EmailSender],
    stats: EmailStats,
) -> bool:
    for index, target in enumerate(targets):
        sender = senders.get(target.consent_status or "")
        if sender is None:
            # Unreachable by construction — the claim filters on the
            # configured consents — kept as defense in depth.
            log.error("no sender for consent %r (contact %d), releasing",
                      target.consent_status, target.id)
            stats.errors.append(f"no sender for consent {target.consent_status!r}")
            stats.released += await repo.release_email_claims([target.id])
            continue

        try:
            ref = await sender.send(target)
        except SendRejected as exc:
            await repo.mark_email_failed(target.id, sender.name, str(exc))
            stats.rejected += 1
            log.warning("send rejected for contact %d: %s", target.id, exc)
            continue
        except SendUncertain as exc:
            # The send may have landed and the provider has no idempotency
            # key, so this contact must NOT be released (a replay could
            # double-send). Leave it at 'sending' to be surfaced as stuck
            # and resolved by a human. The untried remainder never left, so
            # release it as normal, and stop the run.
            log.error("send outcome uncertain for contact %d, left at 'sending': %s",
                      target.id, exc)
            stats.errors.append(str(exc))
            stats.uncertain += 1
            rest = [t.id for t in targets[index + 1:]]
            if rest:
                stats.released += await repo.release_email_claims(rest)
            return False
        except ProviderError as exc:
            # Vendor down. Nothing has been marked for the current
            # contact; releasing it is safe because a replayed send
            # dedupes vendor-side. Stop rather than hammering.
            log.error("sender failed, releasing %d contacts: %s",
                      len(targets) - index, exc)
            stats.errors.append(str(exc))
            stats.released += await repo.release_email_claims(
                [t.id for t in targets[index:]]
            )
            return False

        # Written immediately — never batched — so a crash cannot lose
        # more than the one in-flight send.
        await repo.mark_email_sent(target.id, sender.name, ref)
        stats.sent += 1

    return True


async def run(senders: dict[str, EmailSender] | None = None,
              max_passes: int | None = None) -> EmailStats:
    senders = senders if senders is not None else build_senders()
    started = time.monotonic()
    stats = EmailStats()
    limit = max_passes or config.MAX_PASSES

    stats.stuck_sending = await repo.count_stuck_sending()
    if stats.stuck_sending:
        log.warning(
            "%d contacts stuck at 'sending' — their emails may have gone out "
            "before a crash; resolve manually against the provider dashboard "
            "(never auto-reset).", stats.stuck_sending,
        )

    stats.suppressed = await repo.sweep_suppressed()
    if stats.suppressed:
        log.info("suppressed %d contacts before sending", stats.suppressed)

    consents = sorted(senders)
    while stats.passes < limit:
        targets = await repo.claim_email_batch(consents)
        if not targets:
            break

        stats.passes += 1
        stats.claimed += len(targets)
        log.info("pass %d: claimed %d for sending", stats.passes, len(targets))

        if not await _send_batch(targets, senders, stats):
            break

        if len(targets) < config.SEND_BATCH_SIZE:
            break

    stats.seconds = time.monotonic() - started
    log.info("email run complete: %s", stats.as_dict())
    return stats
