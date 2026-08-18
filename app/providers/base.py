"""The contract every enrichment provider implements.

The runner never imports a vendor module directly. It walks a list of
Provider objects and only sees EnrichmentResult, so adding, reordering or
replacing a vendor is a config change rather than a code change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class ProviderError(Exception):
    """Provider failed for reasons unrelated to any specific contact.

    Rate limits, timeouts, 5xx, malformed responses. The runner returns
    the affected contacts to 'pending' rather than marking them failed,
    because "the vendor was down" is not "this person has no LinkedIn".
    """


@dataclass(slots=True)
class EnrichmentResult:
    email: str
    linkedin_url: str | None
    confidence: float  # always normalised to 0.0–1.0
    tier: str          # which tier resolved it, for the events log


@dataclass(slots=True)
class Draft:
    subject: str
    body: str
    linkedin_note: str | None


class DraftRefused(Exception):
    """The model declined this one contact. An outcome for that contact,
    not a vendor failure — the run releases the contact and continues."""


@runtime_checkable
class Drafter(Protocol):
    """What the drafting runner needs from a vendor. Swapping vendors is
    a config change (DRAFT_PROVIDER), not a code change."""

    name: str

    async def draft(self, system: str, user: str) -> Draft: ...


# The drafting output contract, appended to the system prompt by every
# JSON-mode drafter (currently the n8n webhook): the model returns a bare
# JSON object, and JSON mode guarantees only syntax, so the shape is pinned
# here and validated on parse. Lives on the neutral base, not any one
# vendor, so a provider swap never re-homes it.
FORMAT_INSTRUCTION = (
    "\n\nRespond with a single JSON object of exactly this form: "
    '{"subject": string, "body": string, "linkedin_note": string}. '
    "No other keys, no markdown fences."
)


class SendRejected(Exception):
    """The vendor hard-rejected this one contact's send (invalid
    recipient, blocklist). An outcome for that contact, not a vendor
    failure — the run marks it failed and continues."""


class SendUncertain(Exception):
    """The send may or may not have landed — an ambiguous failure AFTER
    the request left us (a read timeout, a broken response). With a
    provider that has no idempotency key (Mailjet), we cannot retry or
    release: either could double-send. The runner leaves the contact at
    'sending' to be surfaced as stuck and resolved by a human, upholding
    invariant #1 (at most one first-touch email per contact, ever)."""


@runtime_checkable
class EmailSender(Protocol):
    """What the email runner needs from a delivery vendor. Returns a
    provider reference for the audit event. Implementations must be
    idempotent per contact (retrying a send that may already have
    landed must not produce a second email)."""

    name: str

    async def send(self, target) -> str: ...


@runtime_checkable
class Provider(Protocol):
    name: str
    #: Max records the vendor accepts per request. The runner chunks to this.
    batch_size: int



def normalise_confidence(raw: float | int | str | None) -> float:
    if raw is None:
        return 0.0
    if isinstance(raw, str):
        bucket = {"very high": 0.95, "high": 0.85, "moderate": 0.6,
                  "medium": 0.6, "low": 0.3, "very low": 0.1}
        key = raw.strip().lower()
        if key in bucket:
            return bucket[key]
        try:
            raw = float(raw)
        except ValueError:
            return 0.0
    value = float(raw)
    if value > 1.0:  # percentage scale
        value = value / 100.0
    return max(0.0, min(1.0, round(value, 2)))