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
    csv_drafted: int = 0
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
            "csv_drafted": self.csv_drafted,
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


# The built-in default prompt: the Agency Value Calculator outreach. Used
# whenever a campaign has no stored objective (every pre-018 campaign), so
# existing behaviour is unchanged until an operator writes one. The one
# load-bearing mechanic is LINK FORMATTING: the model emits a real
# <a href="https://…"> anchor inline, which email_format.render_html_body
# passes through un-escaped (a single strict https anchor only) and the
# plain-text part strips to "text (url)".
DEFAULT_SYSTEM_PROMPT = (
    "You write short, personalized first-touch cold emails on the sender's "
    "behalf to independent insurance agency owners and principals. "
    "Personalize only from the supplied prospect data, and never invent a "
    "detail you were not given or mention how the data was obtained.\n\n"
    "Write a short cold email between 60 and 90 words. Shorter is better.\n\n"
    "Structure:\n"
    "1. One or two sentences of personalization built on a concrete FACT from "
    "the supplied data (producer count, book size, market, tenure, ownership), "
    "pointed at value: what that fact means for what the agency is worth. "
    "Never compliment or evaluate the prospect. Words like \"impressive\", "
    "\"solid\", or \"great\" end on your opinion; end on the fact's implication "
    "instead. Example: \"With 13 producers at Acme Insurance, you're at the "
    "size where owners start asking what the book is actually worth.\"\n"
    "2. One or two sentences pitching the Agency Value Calculator: it gives "
    "an estimated current-market valuation in about 60 seconds, no sales "
    "call required.\n"
    "3. One short closing line inviting a reply if the result raises "
    "questions.\n\n"
    "Format the body as two or three short paragraphs separated by blank "
    "lines — the personalization, the calculator pitch with the link, and "
    "the closing line. Never write the whole email as one paragraph.\n\n"
    "Do not explain the full list of valuation factors. You may mention at "
    "most one factor (such as retention or carrier mix) if it fits the "
    "personalization naturally.\n\n"
    "Use the recipient's first name.\n\n"
    "Create one subject line with no more than 45 characters.\n\n"
    "LINK FORMATTING\n\n"
    "The email body will be sent as HTML. Include the calculator link "
    "exactly once, embedded as a hyperlink inside a natural sentence using "
    "an HTML anchor tag, like this:\n\n"
    "If you're curious what your book is worth in today's market, "
    "<a href=\"https://renegadeinsurance.com/agency-value-calculator/"
    "?utm_source=automation\">take a quick look here</a>.\n\n"
    "Never paste the raw URL as visible text. Never use markdown link "
    "syntax like [text](url). Use only the anchor tag format shown above. "
    "Vary the anchor text naturally between emails (examples: \"check your "
    "agency's current value\", \"run the 60-second estimate\", \"see where "
    "your agency stands\").\n\n"
    "Do not use em dashes.\n"
    "Do not use excessive punctuation.\n"
    "Do not include bullet points.\n"
    "Do not include any markdown formatting.\n"
    "Do not generate the sender's signature. The signature will be added "
    "separately by the automation."
)

# The fixed mechanical scaffold, appended to EVERY system prompt (a campaign's
# stored objective, or the default above). These rules are load-bearing — the
# pipeline breaks or the mail embarrasses the sender without them — so they
# hold no matter what the objective says: paragraphs feed the HTML renderer,
# only a strict https anchor survives it, invented details are the one
# unforgivable content bug, internal list fields must never leak to the
# recipient (a live "Tier 1 market" slipped into copy), a malformed company
# name must not lead the email ("Bb Insurance Marketing" did), and the
# signature is appended at send time (anything the model adds would
# double-sign). Everything else — audience, product, structure, tone, links —
# belongs to the objective.
PROMPT_SCAFFOLD = (
    "MECHANICAL RULES (these override anything above):\n"
    "- Personalize only from the supplied prospect data. Never invent a "
    "detail you were not given, and never mention how the data was obtained.\n"
    "- The prospect data may include internal list fields (tiers, scores, "
    "ranks, segments, lead source, notes). Never mention or hint at these — "
    "reference only facts the recipient already knows about themselves, such "
    "as their market, role, team size, or location.\n"
    "- If a company or agency name in the data looks malformed, truncated, or "
    "like a placeholder, do not use it verbatim; say \"your agency\" instead.\n"
    "- Format the body as two or three short paragraphs separated by blank "
    "lines. Never write the whole email as one paragraph.\n"
    "- Any link must appear exactly once, embedded in a natural sentence as "
    "an HTML anchor tag: <a href=\"https://...\">anchor text</a>. Never paste "
    "a raw URL as visible text, and never use markdown link syntax.\n"
    "- Do not include any markdown formatting.\n"
    "- Write one subject line as well.\n"
    "- Do not write any closing, sign-off, or signature — no name, title, "
    "company, or contact details at the end. The signature is appended "
    "automatically by the automation."
)

# Company strings that are a whole generic word, a placeholder, or carry a
# truncated/auto-capitalised token ("Bb ...") are source-data defects: leading
# the email with one is worse than not naming the company at all, so the
# prospect block falls back to 'unknown' and the model personalizes from role,
# market, or size instead (both prompts instruct that for sparse data).
_GENERIC_COMPANY = {
    "insurance", "agency", "company", "llc", "inc", "corp", "corporation",
    "unknown", "none", "null", "test", "sample", "tbd", "n/a", "na",
}
_PLACEHOLDER_TOKENS = {"null", "n/a", "na", "unknown", "none", "test",
                       "sample", "tbd"}
# Exactly two letters, same letter, mixed case ("Bb", "Ss"): the classic
# auto-capitalised truncation defect. Deliberately NOT flagging all-caps
# acronyms ("AA Insurance") or real particles ("La Familia Insurance").
_DEFECT_TOKEN_RE = re.compile(r"^([A-Z])([a-z])$")


def suspicious_company(name: str | None) -> bool:
    """True when a company name looks like a source-data defect rather than a
    real name — conservative on purpose: a false positive only costs one
    personalization hook, a false negative puts garbage in the first line."""
    name = (name or "").strip()
    if not name:
        return False                       # absent is handled as 'unknown' anyway
    if len(name) < 3 or len(name) > 60:
        return True
    if not any(ch.isalpha() for ch in name):
        return True
    tokens = name.split()
    if len(tokens) == 1 and tokens[0].lower().strip(".,&") in _GENERIC_COMPANY:
        return True
    for token in tokens:
        if token.lower() in _PLACEHOLDER_TOKENS:
            return True
        match = _DEFECT_TOKEN_RE.fullmatch(token)
        if match and match.group(1).lower() == match.group(2):
            return True
    return False


def _prospect_block(target: "repo.DraftTarget") -> str:
    """The shared Prospect header of the user prompt. A company name that fails
    the sanity check is withheld (shown as 'unknown') so a data defect can
    never lead the email; the scaffold's malformed-name rule is the backstop
    for the copy of the name inside the raw profile/sheet JSON."""
    company = (target.company or "").strip()
    if suspicious_company(company):
        company = ""
    return (
        "Prospect:\n"
        f"- Name: {target.first_name} {target.last_name or ''}\n"
        f"- Title: {target.title or 'unknown'}\n"
        f"- Company: {company or 'unknown'}\n"
    )


def build_prompts(target: "repo.DraftTarget") -> tuple[str, str]:
    # The campaign's stored objective IS its drafting prompt (migration 018);
    # a campaign without one gets the built-in default. Either way the fixed
    # mechanical scaffold is appended — its rules hold for every campaign.
    # Editable on the campaign page, so prompt iteration is edit -> test email
    # -> release, with no code change per campaign.
    campaign = target.campaign or {}
    objective = str(campaign.get("objective") or "").strip()
    system = (objective or DEFAULT_SYSTEM_PROMPT) + "\n\n" + PROMPT_SCAFFOLD

    profile = json.dumps(target.profile_data or {}, ensure_ascii=False)
    if len(profile) > config.DRAFT_PROFILE_CHAR_LIMIT:
        # A truncated JSON tail is fine: the useful signal (headline,
        # current role, recent experience) serialises first in practice,
        # and the prompt says the data may be cut off.
        profile = profile[: config.DRAFT_PROFILE_CHAR_LIMIT]

    user = (
        _prospect_block(target)
        + f"\nLinkedIn profile data (JSON, may be truncated):\n{profile}"
    )
    return system, user


def build_csv_prompts(target: "repo.DraftTarget") -> tuple[str, str]:
    """The CSV-only counterpart to build_prompts: same system prompt, but the
    user block personalizes from the contact's captured sheet columns
    (extra_data) instead of a scraped LinkedIn profile. Reuses build_prompts
    for the system so the voice/rules stay identical."""
    system, _ = build_prompts(target)
    extra = json.dumps(target.extra_data or {}, ensure_ascii=False)
    if len(extra) > config.DRAFT_PROFILE_CHAR_LIMIT:
        # Same truncation rationale as the profile JSON — the useful signal
        # serialises first and the prompt says the data may be cut off.
        extra = extra[: config.DRAFT_PROFILE_CHAR_LIMIT]
    user = (
        _prospect_block(target)
        + "\nAdditional details from the uploaded list (JSON, may be "
        "truncated) — personalize from these where they help:\n"
        f"{extra}"
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
    # Sheet columns a CSV-only campaign would carry, so a 'csv'-mode preview
    # personalizes from these (via build_csv_prompts) rather than the profile.
    "extra_data": {
        "state": "TX",
        "website": "reyesinsurance.com",
        "lines_of_business": "commercial P&C, small-business",
        "notes": "Independent agency, ~12 years in business.",
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
        extra_data=PREVIEW_SAMPLE["extra_data"],
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
    # A 'csv' campaign has no fallback-template path — every contact is drafted
    # from the sheet columns — so the UI shows only the personalised email, not
    # the "no-profile fallback" card. Computed up front so it rides on every
    # return path (including the unconfigured/no-profile early returns).
    csv_mode = (brief.get("enrichment_mode") or "linkedin") == "csv"
    result = {
        "from": {"name": brief.get("sender"), "role": brief.get("sender_role")},
        "csv_mode": csv_mode,
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
            "Drafting isn't configured (" + ", ".join(missing) + "), so the "
            + ("email can't be previewed" if csv_mode
               else "personalised email can't be shown — only the fallback template")
            + " — set the draft provider to preview it."
        )
        return result

    # Mirror production exactly (same gate as _draft_batch). A 'csv' campaign
    # personalises from the sheet columns (no profile needed); otherwise only
    # a contact with a scraped profile gets the LLM email — a no-profile
    # LinkedIn contact would receive the fallback template, so the preview
    # shows that rather than fabricating an email the contact never gets.
    if not csv_mode and not target.profile_data:
        result["note"] = ("This contact hasn't been scraped yet, so production "
                          "would send the fallback template below — it becomes "
                          "personalised once the LinkedIn profile is in.")
        return result

    try:
        drafter = drafter or build_drafter()
        system, user = build_csv_prompts(target) if csv_mode else build_prompts(target)
        draft = await drafter.draft(system, user)
        result["personalized"] = {
            "subject": draft.subject,
            "body": draft.body,
            # csv campaigns send email only — there's no LinkedIn note.
            "linkedin_note": None if csv_mode else clamp_note(draft.linkedin_note),
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
        campaign = target.campaign or {}
        # Mode-first gate. A 'csv' campaign always personalizes from the sheet
        # (its contacts never have a profile); otherwise a scraped profile
        # gets the LinkedIn LLM path, and a no-profile LinkedIn contact gets
        # the static fallback template (unchanged, no LLM).
        if campaign.get("enrichment_mode") == "csv":
            system, user = build_csv_prompts(target)
            path, keep_note = "csv", False
        elif target.profile_data:
            system, user = build_prompts(target)
            path, keep_note = "llm", True
        else:
            fields = merge_fields(target)
            results.append({
                "id": target.id,
                "email_subject": render_template(campaign.get("fallback_email_subject"), fields),
                "email_body": render_template(campaign.get("fallback_email_body"), fields),
                "linkedin_note": None,
                "path": "template",
            })
            stats.template_drafted += 1
            continue

        try:
            draft = await drafter.draft(system, user)
            if keep_note and draft.linkedin_note and len(draft.linkedin_note) > NOTE_MAX_CHARS:
                # One corrective retry beats silently truncating a note
                # that will be read by a person; clamp only if the model
                # overruns twice. (csv drafts discard the note — no retry.)
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
            "linkedin_note": clamp_note(draft.linkedin_note) if keep_note else None,
            "path": path,
        })
        if path == "csv":
            stats.csv_drafted += 1
        else:
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
