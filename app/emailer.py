"""Stage 4 runner: email sending.

Mailjet is the only send provider. Every approved draft goes out through
it, rotating its From through the global sender pool (repo.mailjet_senders)
so no single domain carries all the cold volume — unless the campaign is
pinned to one mailbox (repo.EmailTarget.pinned_sender_id), in which case
every send for it draws that one sender instead.

Business-hours drip — this is the pacing model, not a burst:

- One run sends exactly ONE batch (one email per active mailbox, via LRU
  rotation) and stops. The scheduler tick (SCHEDULER_INTERVAL_MINUTES,
  default 5) is the cadence, so an approved list drains a batch at a time.
- Sends only inside the SEND_WINDOW_* hours (default 9–5 America/New_York,
  weekdays); outside it the run is a clean no-op (see within_send_window).
- The per-sender daily cap is off by default (daily_cap <= 0 = unlimited);
  the batch size + tick interval + window ARE the throttle. A positive
  daily_cap still caps an individual mailbox if an operator sets one.

Money-grade failure semantics — the business rule is at most ONE first-touch
email per contact, ever:

- Results are written per contact, immediately after the vendor accepts,
  not batched: a crash window of milliseconds instead of a batch.
- Stale 'sending' claims are NEVER auto-reset. A crash after the vendor
  accepted but before our write leaves rows whose email went out;
  resetting them to 'drafted' would re-send. They are counted, surfaced
  in stats and the dashboard, and resolved by a human.
- Mailjet has NO idempotency key, so its provider raises SendUncertain for
  ambiguous post-send failures — the contact is left at 'sending'
  (surfaced, never released) rather than risk a re-send.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from zoneinfo import ZoneInfo

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
    window_skipped: bool = False
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
            "window_skipped": self.window_skipped,
            "seconds": round(self.seconds, 1),
            "errors": self.errors,
        }


def build_sender() -> EmailSender | None:
    """The Mailjet sender, or None when its key pair isn't configured — in
    which case the stage no-ops (nothing is claimed)."""
    if config.MAILJET_API_KEY and config.MAILJET_SECRET_KEY:
        from .providers.mailjet import MailjetSender
        return MailjetSender()
    return None


async def sync_pool(sender: EmailSender | None = None) -> dict:
    """Best-effort: make the rotation pool mirror Mailjet's verified senders
    (full auto-enrol) — new verified addresses join at the default daily
    cap, addresses no longer verified are paused. Called before every send
    run and on the admin pages so the operator manages senders in Mailjet,
    not here.

    Swallowed on failure — a Mailjet outage, unset keys, or a sender that
    cannot list (a test fake) returns an error in the dict and leaves the
    existing pool exactly as it was. Crucially it does NOT reconcile against
    an empty list on a Mailjet error (which would pause the whole pool): the
    ProviderError short-circuits before repo.sync_senders_from_mailjet is
    reached. Returns ``{"inserted", "deactivated", "error"}``.
    """
    if sender is None:
        sender = build_sender()
    if sender is None or not hasattr(sender, "list_verified_sender_records"):
        return {"inserted": 0, "deactivated": 0, "error": "sender pool sync unavailable"}
    try:
        records = await sender.list_verified_sender_records()
    except ProviderError as exc:
        log.warning("sender pool sync skipped: %s", exc)
        return {"inserted": 0, "deactivated": 0, "error": str(exc)}
    # Only allowlisted addresses may rotate: drop any other verified address
    # so it never enrols. Since the sync pauses active rows NOT in the incoming
    # list, this also auto-pauses any non-approved row already in the pool
    # (see config.SENDER_ALLOWED_ADDRESSES; empty = no restriction).
    records = [r for r in records if config.sender_allowed(r.get("email"))]
    result = await repo.sync_senders_from_mailjet(
        records, config.MAILJET_SENDER_DAILY_CAP)
    if result["inserted"] or result["deactivated"]:
        log.info("sender pool synced from Mailjet: +%d enrolled, %d paused (unverified)",
                 result["inserted"], result["deactivated"])
    return {**result, "error": None}


def within_send_window(now: datetime) -> bool:
    """True when `now` (tz-aware, in SEND_WINDOW_TZ) falls inside the
    business-hours send window. Weekends are excluded when
    SEND_WINDOW_WEEKDAYS_ONLY. Pure and time-injectable so tests never depend
    on the wall clock; the start hour is inclusive, the end hour exclusive
    (so a batch can start at 16:59 but never at 17:00)."""
    if config.SEND_WINDOW_WEEKDAYS_ONLY and now.weekday() >= 5:
        return False
    return config.SEND_WINDOW_START_HOUR <= now.hour < config.SEND_WINDOW_END_HOUR


def _with_signature(body: str, signature: str | None) -> str:
    """Append the sending address's fixed signature block to the drafted body.
    The drafter writes no closing, so this is the message's only sign-off. A
    no-op when the sender has no signature set (nothing to append)."""
    if not signature or not signature.strip():
        return body
    return body.rstrip() + "\n\n" + signature.strip()


async def _send_batch(
    targets: list["repo.EmailTarget"],
    sender: EmailSender,
    stats: EmailStats,
) -> bool:
    for index, target in enumerate(targets):
        # Draw the From, stamped on the target. A campaign pinned to one
        # mailbox (pinned_sender_id) draws that sender only; every other
        # campaign draws the least-recently-used sender from the global
        # rotation pool. Both count against the per-domain daily cap.
        if target.pinned_sender_id is not None:
            picked = await repo.claim_pinned_sender(target.pinned_sender_id)
            if picked is None:
                # This campaign's one mailbox is at its daily cap. Do NOT
                # pause the whole run — that would stall other campaigns in
                # the same batch. Return just this contact to 'drafted' (it
                # sends when the cap resets; CLAIM_EMAIL_SQL excludes it in
                # the meantime) and move on.
                stats.released += await repo.release_email_claims([target.id])
                continue
        else:
            picked = await repo.claim_rotating_sender()
            if picked is None:
                # The whole pool is at its daily cap — pause the run (release
                # the untried remainder); it resumes when caps reset.
                log.warning("rotation pool at daily cap — pausing sends")
                stats.errors.append("rotation pool at daily cap")
                stats.released += await repo.release_email_claims(
                    [t.id for t in targets[index:]]
                )
                return False
        # Stamp the From and append that sending address's fixed signature
        # (the drafter writes no closing). Keyed on the actual From, so a
        # rotating campaign signs each mail as whoever it went out from.
        target = replace(
            target, sender_email=picked["sender_email"],
            sender_name=picked["sender_name"],
            email_body=_with_signature(target.email_body, picked.get("signature")),
        )

        try:
            ref = await sender.send(target)
        except SendRejected as exc:
            # The mail did NOT go out — give the sender its daily slot back
            # (release_rotating_sender is keyed on email, so it serves the
            # pinned pick too).
            await repo.release_rotating_sender(picked["sender_email"])
            await repo.mark_email_failed(target.id, sender.name, str(exc))
            stats.rejected += 1
            log.warning("send rejected for contact %d: %s", target.id, exc)
            continue
        except SendUncertain as exc:
            # The send may have landed and the provider has no idempotency
            # key, so this contact must NOT be released (a replay could
            # double-send). Leave it at 'sending' to be surfaced as stuck
            # and resolved by a human. The untried remainder never left, so
            # release it as normal, and stop the run. The rotating slot
            # stands (the mail may have counted).
            log.error("send outcome uncertain for contact %d, left at 'sending': %s",
                      target.id, exc)
            stats.errors.append(str(exc))
            stats.uncertain += 1
            rest = [t.id for t in targets[index + 1:]]
            if rest:
                stats.released += await repo.release_email_claims(rest)
            return False
        except ProviderError as exc:
            # Vendor down. Nothing has been marked for the current contact;
            # releasing it is safe. Stop rather than hammering. Only the
            # current contact drew a sender (rotating or pinned) — give it
            # back (the untried remainder never picked one).
            await repo.release_rotating_sender(picked["sender_email"])
            log.error("sender failed, releasing %d contacts: %s",
                      len(targets) - index, exc)
            stats.errors.append(str(exc))
            stats.released += await repo.release_email_claims(
                [t.id for t in targets[index:]]
            )
            return False

        # Written immediately — never batched — so a crash cannot lose
        # more than the one in-flight send. Record the From that was drawn so
        # the Schedule log can group sends by mailbox.
        await repo.mark_email_sent(target.id, sender.name, ref, target.sender_email)
        stats.sent += 1

    return True


async def run(sender: EmailSender | None = None,
              now: datetime | None = None) -> EmailStats:
    """Send ONE drip batch — one email per active mailbox — then stop. The
    5-minute scheduler tick is the cadence: each run drains a single batch, so
    an approved list goes out steadily rather than in one burst. Outside the
    business-hours send window the run is a clean no-op (`now` is injectable so
    tests never touch the wall clock)."""
    sender = sender if sender is not None else build_sender()
    started = time.monotonic()
    stats = EmailStats()

    # Gate on the send window first, before any DB read/write, so off-hours is
    # a true no-op. Weekends/nights simply skip; the next in-window tick sends.
    if config.SEND_WINDOW_ENABLED:
        now = now or datetime.now(ZoneInfo(config.SEND_WINDOW_TZ))
        if not within_send_window(now):
            log.info("email: %s is outside the send window (%02d:00–%02d:00 %s%s) — skipping",
                     now.isoformat(timespec="minutes"), config.SEND_WINDOW_START_HOUR,
                     config.SEND_WINDOW_END_HOUR, config.SEND_WINDOW_TZ,
                     ", weekdays only" if config.SEND_WINDOW_WEEKDAYS_ONLY else "")
            stats.window_skipped = True
            stats.seconds = time.monotonic() - started
            return stats

    stats.stuck_sending = await repo.count_stuck_sending()
    if stats.stuck_sending:
        log.warning(
            "%d contacts stuck at 'sending' — their emails may have gone out "
            "before a crash; resolve manually against the Mailjet dashboard "
            "(never auto-reset).", stats.stuck_sending,
        )

    stats.suppressed = await repo.sweep_suppressed()
    if stats.suppressed:
        log.info("suppressed %d contacts before sending", stats.suppressed)

    if sender is None:
        log.info("email: Mailjet not configured — nothing to send")
        stats.seconds = time.monotonic() - started
        return stats

    # Refresh the From-rotation pool from Mailjet's verified senders before
    # claiming, so a domain validated in Mailjet is usable this pass without
    # any manual step. Best-effort: a Mailjet hiccup leaves the last-synced
    # pool in place (see sync_pool) and the run proceeds.
    await sync_pool(sender)

    # One batch = one send per active mailbox. Sizing the claim to the active
    # count and drawing the least-recently-used sender per send (claim_rotating_
    # sender) makes each mailbox send exactly once; SEND_BATCH_SIZE only clamps
    # an unexpectedly large pool. Empty pool → nothing to rotate through.
    active = await repo.count_active_senders()
    if active <= 0:
        log.info("email: no active senders — nothing to send")
        stats.seconds = time.monotonic() - started
        return stats

    targets = await repo.claim_email_batch(limit=min(active, config.SEND_BATCH_SIZE))
    if targets:
        stats.passes = 1
        stats.claimed = len(targets)
        log.info("drip batch: claimed %d for sending (one per active sender)",
                 len(targets))
        await _send_batch(targets, sender, stats)

    stats.seconds = time.monotonic() - started
    log.info("email run complete: %s", stats.as_dict())
    return stats


async def send_campaign_now(campaign_id: int,
                            sender: EmailSender | None = None) -> EmailStats:
    """Manual override for one campaign: send every approved-but-unsent contact
    right now, draining the queue rather than one drip batch, and WITHOUT the
    business-hours window (that's the point — e.g. warming a mailbox). Every
    other guarantee still holds: the suppression sweep, the one-first-touch
    claim, allowlist/rotation (a pinned campaign sends from its one mailbox),
    the appended signature, and the per-send immediate write with money-grade
    release on failure. Bounded by MAX_PASSES so a click can't run unbounded;
    a larger campaign is drained by clicking again."""
    sender = sender if sender is not None else build_sender()
    started = time.monotonic()
    stats = EmailStats()

    stats.stuck_sending = await repo.count_stuck_sending()
    stats.suppressed = await repo.sweep_suppressed()

    if sender is None:
        log.info("send-now: Mailjet not configured — nothing to send")
        stats.seconds = time.monotonic() - started
        return stats

    await sync_pool(sender)

    while stats.passes < config.MAX_PASSES:
        targets = await repo.claim_email_batch(
            limit=config.SEND_BATCH_SIZE, campaign_id=campaign_id)
        if not targets:
            break
        stats.passes += 1
        stats.claimed += len(targets)
        log.info("send-now campaign %d: claimed %d", campaign_id, len(targets))
        if not await _send_batch(targets, sender, stats):
            break               # vendor down / pool exhausted / uncertain — stop
        if len(targets) < config.SEND_BATCH_SIZE:
            break               # queue drained

    stats.seconds = time.monotonic() - started
    log.info("send-now complete for campaign %d: %s", campaign_id, stats.as_dict())
    return stats
