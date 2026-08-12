"""Stage 3 runner: drafting.

Same skeleton as the enrichment runner. Two paths per contact:

- profile_data present  -> LLM call: personalised email + LinkedIn note.
- no profile_data       -> the campaign's fallback email template with
  merge fields. No LLM call — the output is identical for everyone, so
  generating it N times costs money and adds variance for no benefit.
  No LinkedIn note either: there is no profile to connect to.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

from . import repo
from .config import config
from .providers.base import Drafter, DraftRefused, ProviderError

log = logging.getLogger(__name__)

# Mirrors the CHECK constraint on contacts.linkedin_note. The structured
# output schema cannot enforce length, so it is enforced here — a draft
# that violates the constraint would fail the whole batch write.
NOTE_MAX_CHARS = 300

MERGE_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")


@dataclass
class DraftStats:
    passes: int = 0
    claimed: int = 0
    llm_drafted: int = 0
    template_drafted: int = 0
    refused: int = 0
    released: int = 0
    stale_recovered: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "passes": self.passes,
            "claimed": self.claimed,
            "llm_drafted": self.llm_drafted,
            "template_drafted": self.template_drafted,
            "refused": self.refused,
            "released": self.released,
            "stale_recovered": self.stale_recovered,
            "seconds": round(self.seconds, 1),
            "errors": self.errors,
        }


def merge_fields(target: "repo.DraftTarget") -> dict:
    campaign = target.campaign or {}
    return {
        "first_name": target.first_name,
        "last_name": target.last_name,
        "company": target.company,
        "title": target.title,
        "sender": campaign.get("sender"),
        "sender_role": campaign.get("sender_role"),
        "offer": campaign.get("offer"),
        "cta": campaign.get("cta"),
    }


def render_template(template: str | None, fields: dict) -> str:
    def replace(match: re.Match) -> str:
        return str(fields.get(match.group(1)) or "")

    return MERGE_RE.sub(replace, template or "").strip()


def clamp_note(note: str | None) -> str | None:
    """Last resort after the retry: cut at a word boundary rather than
    mid-word, because this text goes to a real person."""
    if not note or len(note) <= NOTE_MAX_CHARS:
        return note
    cut = note[: NOTE_MAX_CHARS - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:.") + "."


def build_prompts(target: "repo.DraftTarget") -> tuple[str, str]:
    campaign = target.campaign or {}
    brief = [
        ("Offer", campaign.get("offer")),
        ("Call to action", campaign.get("cta")),
        ("Tone", campaign.get("tone")),
        ("Sender", campaign.get("sender")),
        ("Sender's role", campaign.get("sender_role")),
        ("Why this audience", campaign.get("audience_rationale")),
    ]
    brief_lines = "\n".join(f"- {label}: {value}" for label, value in brief if value)

    system = (
        "You write first-touch outreach to insurance agents on the sender's "
        "behalf. Both messages must read like one busy professional writing "
        "to another.\n\n"
        f"Campaign brief:\n{brief_lines}\n\n"
        "Rules:\n"
        "- Reference one or two specific details from the prospect's profile, "
        "so the message could only have been written to them. Never fabricate "
        "a detail that is not in the data; if the profile is sparse, "
        "personalise from role and company instead.\n"
        "- No hype, no flattery, no 'I hope this finds you well', no emoji.\n"
        "- Email: subject under 60 characters; body plain text, under 120 "
        "words, first person, ending with the call to action phrased as a "
        "low-friction question.\n"
        "- LinkedIn note: under 260 characters, no links, references "
        "something specific about them, and does not hard-pitch the offer — "
        "its only job is to make accepting the connection feel natural.\n"
    )

    profile = json.dumps(target.profile_data or {}, ensure_ascii=False)
    if len(profile) > config.DRAFT_PROFILE_CHAR_LIMIT:
        # A truncated JSON tail is fine: the useful signal (headline,
        # current role, recent experience) serialises first in practice,
        # and the prompt says the data may be cut off.
        profile = profile[: config.DRAFT_PROFILE_CHAR_LIMIT]

    user = (
        "Prospect:\n"
        f"- Name: {target.first_name} {target.last_name or ''}\n"
        f"- Title: {target.title or 'unknown'}\n"
        f"- Company: {target.company or 'unknown'}\n\n"
        f"LinkedIn profile data (JSON, may be truncated):\n{profile}"
    )
    return system, user


def build_drafter() -> Drafter:
    """Instantiate the configured draft vendor — a config change, not a
    code change, same rule as the enrichment provider."""
    if config.DRAFT_PROVIDER == "n8n":
        from .providers.n8n_llm import N8nDrafter
        return N8nDrafter()
    if config.DRAFT_PROVIDER == "groq":
        from .providers.groq import GroqDrafter
        return GroqDrafter()
    if config.DRAFT_PROVIDER == "anthropic":
        from .providers.anthropic import AnthropicDrafter
        return AnthropicDrafter()
    raise RuntimeError(f"Unknown DRAFT_PROVIDER: {config.DRAFT_PROVIDER!r}")


async def _draft_batch(
    targets: list["repo.DraftTarget"],
    drafter: Drafter,
    stats: DraftStats,
) -> bool:
    results: list[dict] = []

    for index, target in enumerate(targets):
        if not target.profile_data:
            fields = merge_fields(target)
            campaign = target.campaign or {}
            results.append({
                "id": target.id,
                "email_subject": render_template(campaign.get("fallback_email_subject"), fields),
                "email_body": render_template(campaign.get("fallback_email_body"), fields),
                "linkedin_note": None,
                "path": "template",
            })
            stats.template_drafted += 1
            continue

        system, user = build_prompts(target)
        try:
            draft = await drafter.draft(system, user)
            if draft.linkedin_note and len(draft.linkedin_note) > NOTE_MAX_CHARS:
                # One corrective retry beats silently truncating a note
                # that will be read by a person; clamp only if the model
                # overruns twice.
                draft = await drafter.draft(
                    system,
                    user
                    + "\n\nYour previous LinkedIn note was too long. The note "
                    f"MUST be under {NOTE_MAX_CHARS - 40} characters.",
                )
        except DraftRefused:
            # This contact's outcome, not the vendor's failure: release
            # just them and keep drafting the rest.
            stats.refused += 1
            stats.released += await repo.release_draft_claims([target.id])
            log.warning("draft refused for contact %d, released", target.id)
            continue
        except ProviderError as exc:
            # Vendor unavailable. Keep what has been paid for, release
            # everything not yet processed, and stop — continuing would
            # re-claim the same contacts and fail again.
            log.error("drafter failed, releasing %d contacts: %s",
                      len(targets) - index, exc)
            stats.errors.append(str(exc))
            stats.released += await repo.release_draft_claims(
                [t.id for t in targets[index:]]
            )
            if results:
                await repo.write_drafts(results)
            return False

        results.append({
            "id": target.id,
            "email_subject": draft.subject,
            "email_body": draft.body,
            "linkedin_note": clamp_note(draft.linkedin_note),
            "path": "llm",
        })
        stats.llm_drafted += 1

    await repo.write_drafts(results)
    return True


async def run(drafter: Drafter | None = None,
              max_passes: int | None = None) -> DraftStats:
    drafter = drafter or build_drafter()
    started = time.monotonic()
    stats = DraftStats()
    limit = max_passes or config.MAX_PASSES

    stats.stale_recovered = await repo.reset_stale_draft_claims()
    if stats.stale_recovered:
        log.info("recovered %d stale draft claims", stats.stale_recovered)

    while stats.passes < limit:
        targets = await repo.claim_draft_batch()
        if not targets:
            break

        stats.passes += 1
        stats.claimed += len(targets)
        log.info("pass %d: claimed %d for drafting", stats.passes, len(targets))

        if not await _draft_batch(targets, drafter, stats):
            break

        if len(targets) < config.DRAFT_BATCH_SIZE:
            break

    stats.seconds = time.monotonic() - started
    log.info("draft run complete: %s", stats.as_dict())
    return stats
