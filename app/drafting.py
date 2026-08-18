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
from dataclasses import dataclass, field, replace

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

    # Output shape (the three fields) is pinned by the provider's own
    # format instruction, so this prompt carries only voice, personalisation
    # and per-field rules — never an output format, which would collide.
    system = (
        "You write personalized first-touch cold outreach to insurance agents "
        "on the sender's behalf. Both messages must read like one busy "
        "insurance professional writing to another. Lead data may come from "
        "Apollo, Apify, LinkedIn, company websites, or other provided prospect "
        "data — never mention where the data came from, or that it was sourced "
        "or scraped.\n\n"
        f"Campaign brief:\n{brief_lines}\n\n"
        "Personalization:\n"
        "- Open the email with a short, natural observation about the "
        "prospect's current professional profile — it should sound like a real "
        "person glanced at their profile before reaching out.\n"
        "- Draw only on the supplied prospect data: current role, current "
        "company or agency, insurance specialties, lines of business, market or "
        "geography, a recent role change, agency ownership, years in the "
        "industry, recent LinkedIn activity, company positioning, or relevant "
        "experience.\n"
        "- Never invent a detail that is not in the data. If the profile is "
        "sparse, personalize from role, company, insurance focus, or location "
        "instead.\n"
        "- Good: \"Saw you're leading commercial lines at ABC Insurance and "
        "working with small-business clients.\" Avoid compliments (\"impressive "
        "background\", \"amazing profile\"), lines generic enough to fit "
        "anyone, and repeating their whole bio.\n"
        "\n"
        "Email subject:\n"
        "- Under 50 characters, discreet, professional, natural. No "
        "promotional language, clickbait, or excessive capitalization, and no "
        "words like \"offer\", \"deal\", or \"limited time\".\n"
        "- Style: \"Quick question\", \"Your agency\", \"Regarding your book\", "
        "\"Agency operations\", \"Commercial lines\".\n"
        "\n"
        "Email body:\n"
        "- 60-100 words, plain text, first person. The first sentence carries "
        "the personalized observation, then transition naturally into the "
        "reason for reaching out.\n"
        "- Explain the product or service in one or two sentences, focused on "
        "the problem it solves or the practical value — no feature dumps, no "
        "hype or buzzwords, no forced compliments, no \"I hope this finds you "
        "well\", no emojis, nothing that sounds automated.\n"
        "- End with a low-friction CTA that invites a reply. Follow the "
        "campaign brief's call to action where one is given; otherwise use "
        "something like \"Would it be worth a quick conversation?\", \"Open to "
        "taking a look?\", or \"If this is something you're working on, reply "
        "and I'll send the details.\" Do not ask for a 30-minute meeting in a "
        "first email unless the brief requires it.\n"
        "\n"
        "LinkedIn connection note:\n"
        "- Under 260 characters, no links, no hard pitch, and don't explain "
        "the whole product. Reference one specific detail from their current "
        "profile and keep it conversational and understated — its only job is "
        "to make accepting the connection feel natural.\n"
        "- Example: \"Saw you're leading commercial lines at ABC Insurance and "
        "working with businesses in Texas. I work with insurance agencies too, "
        "so it made sense to connect.\"\n"
        "\n"
        "Produce three things: the email subject, the email body, and the "
        "LinkedIn connection note. Return only the finished copy, no "
        "explanation."
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
    if config.DRAFT_PROVIDER == "anthropic":
        from .providers.anthropic import AnthropicDrafter
        return AnthropicDrafter()
    raise RuntimeError(f"Unknown DRAFT_PROVIDER: {config.DRAFT_PROVIDER!r}")


# The stand-in prospect the campaign "Preview email" button drafts against,
# so a preview works the instant a campaign exists — before any real contact
# is enriched. It carries a profile so the personalised (LLM) path runs; it
# is labelled as a sample in the UI and never persisted.
PREVIEW_SAMPLE = {
    "first_name": "Jordan",
    "last_name": "Reyes",
    "company": "Reyes Insurance Group",
    "title": "Agency Owner / Principal",
    "email": "jordan.reyes@reyesinsurance.com",
    "profile_data": {
        "headline": "Owner at Reyes Insurance Group — independent P&C & commercial lines",
        "location": "Austin, Texas",
        "about": "Built an independent agency over 12 years; focused on "
                 "small-business commercial coverage and personal lines.",
        "experience": [
            {"title": "Owner / Principal Agent", "company": "Reyes Insurance Group",
             "years": "2013–present"},
        ],
    },
}


def build_preview_target(campaign: dict,
                         contact: "repo.DraftTarget | None" = None) -> "repo.DraftTarget":
    """A DraftTarget for the preview. With a real `contact` from the
    campaign, reuse its identity/profile and overlay the on-screen (possibly
    unsaved) brief; otherwise fall back to the synthetic PREVIEW_SAMPLE so a
    preview still works before any contact is ingested. The prompt only reads
    name/title/company/profile_data and the brief."""
    if contact is not None:
        return replace(contact, campaign=campaign or {})
    return repo.DraftTarget(
        id=0,
        email=PREVIEW_SAMPLE["email"],
        first_name=PREVIEW_SAMPLE["first_name"],
        last_name=PREVIEW_SAMPLE["last_name"],
        company=PREVIEW_SAMPLE["company"],
        title=PREVIEW_SAMPLE["title"],
        linkedin_url="https://www.linkedin.com/in/sample",
        profile_data=PREVIEW_SAMPLE["profile_data"],
        campaign=campaign or {},
    )


async def preview_draft(campaign: dict, drafter: Drafter | None = None,
                        contact: "repo.DraftTarget | None" = None) -> dict:
    """One email from an (unsaved) brief, for the campaign "Preview email"
    button. Reuses the exact drafting path so the preview matches production,
    but is DB-free, non-persisting, and NEVER raises: a provider failure
    becomes an on-screen note (a preview must not abort anything).

    With a real `contact` from the campaign, the preview is that contact's
    actual email; without one (no contacts ingested yet) it falls back to the
    synthetic sample. `campaign` is the aliased brief dict
    (offer/cta/tone/sender/…), same shape as DraftTarget.campaign. Returns
    the recipient identity, the fallback template rendered for it (the
    no-profile path — free, no LLM), and the personalised email (the LLM
    path) or None with an `error`/`note` explaining why.
    """
    target = build_preview_target(campaign, contact)
    fields = merge_fields(target)
    brief = campaign or {}
    result = {
        "from": {"name": brief.get("sender"), "role": brief.get("sender_role")},
        "source": "contact" if contact is not None else "sample",
        "sample": {
            "name": f"{target.first_name} {target.last_name or ''}".strip(),
            "title": target.title,
            "company": target.company,
            "email": target.email,
        },
        "template": {
            "subject": render_template(brief.get("fallback_email_subject"), fields),
            "body": render_template(brief.get("fallback_email_body"), fields),
        },
        "personalized": None,
        "error": None,
        "note": None,
    }

    missing = config.missing_draft_vars()
    if missing:
        result["error"] = (
            "Drafting isn't configured (" + ", ".join(missing) + "), so only the "
            "fallback template is shown — set the draft provider to preview the "
            "personalised email."
        )
        return result

    # Mirror production exactly: only a contact with a scraped profile gets
    # the LLM personalised email; a no-profile contact would receive the
    # fallback template, so the preview shows that rather than fabricating an
    # LLM email the contact would never actually get.
    if not target.profile_data:
        result["note"] = ("This contact hasn't been scraped yet, so production "
                          "would send the fallback template below — it becomes "
                          "personalised once the LinkedIn profile is in.")
        return result

    try:
        drafter = drafter or build_drafter()
        system, user = build_prompts(target)
        draft = await drafter.draft(system, user)
        result["personalized"] = {
            "subject": draft.subject,
            "body": draft.body,
            "linkedin_note": clamp_note(draft.linkedin_note),
        }
    except DraftRefused:
        result["error"] = ("The model declined to draft this email — try "
                           "adjusting the brief and previewing again.")
    except ProviderError as exc:
        log.warning("preview draft: %s", exc)
        result["error"] = ("The drafting model didn't respond — try again in a "
                           "moment, or check the LLM configuration.")
    return result


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
