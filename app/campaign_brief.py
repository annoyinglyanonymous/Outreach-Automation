"""Campaign quick-create: expand a one-paragraph objective into the full
campaign brief (offer, CTA, tone, audience rationale) plus the fallback
email template.

No HTTP here — the transport lives in providers/n8n_llm.py, mirroring the
drafting.py split. Expansion is best-effort by contract: ANY failure
(provider down, wrong shape, no URL, non-n8n DRAFT_PROVIDER) degrades to
`fallback_brief`, because campaign creation must never fail on a vendor.
"""
from __future__ import annotations

import logging

from .config import config

log = logging.getLogger(__name__)

EXPANSION_FIELDS = (
    "offer_description", "cta", "tone", "audience_rationale",
    "fallback_email_subject", "fallback_email_body",
)

# Only offer_description and the fallback template are load-bearing for
# the pipeline; the LLM may return empty strings for the style fields.
REQUIRED_FIELDS = ("offer_description", "fallback_email_subject", "fallback_email_body")

# Merge fields must be ones drafting.merge_fields() actually provides. No
# sign-off / {{sender}} line: the sending address's signature is appended
# automatically at send time (emailer._with_signature), so a closing here
# would double up.
GENERIC_FALLBACK_SUBJECT = "Quick question for {{company}}"
GENERIC_FALLBACK_BODY = (
    "Hi {{first_name}},\n\n"
    "I work with independent insurance agencies and thought {{company}} "
    "might be a fit for what we do. Worth a short call to find out?"
)

SYSTEM_PROMPT = (
    "You write briefs for cold-email campaigns. Given a campaign objective, "
    "produce the brief a copywriter would work from. Capture the audience, the "
    "offer, and the goal directly from the objective — the brief must reflect "
    "THIS specific objective. Do not add a product, audience, industry, or "
    "claim the objective does not state, do not assume a default vertical, and "
    "do not water it down into something generic.\n\n"
    "The fallback email is sent verbatim (after merge-field substitution) "
    "to contacts we could not research individually, so it must be short, "
    "plain and human, and true to the objective — no hype, no placeholders "
    "other than the merge fields {{first_name}} and {{company}}. Do NOT "
    "include a closing, sign-off, or signature — end at the call to action; a "
    "signature is appended automatically.\n\n"
    "Respond with a single JSON object of exactly this form: "
    '{"offer_description": string, "cta": string, "tone": string, '
    '"audience_rationale": string, "fallback_email_subject": string, '
    '"fallback_email_body": string}. No other keys, no markdown fences.'
)


def fallback_brief(objective: str) -> dict:
    """The no-LLM degradation: the objective verbatim as the offer, a
    generic merge-field template, style fields left for the edit page.
    Empty strings, not None: the live schema marks brief columns NOT
    NULL (found the hard way — cta violated on first live create)."""
    return {
        "offer_description": objective,
        "cta": "",
        "tone": "",
        "audience_rationale": "",
        "fallback_email_subject": GENERIC_FALLBACK_SUBJECT,
        "fallback_email_body": GENERIC_FALLBACK_BODY,
    }


async def expand_objective(objective: str, sender_name: str | None,
                           sender_role: str | None,
                           expander=None) -> tuple[dict, str]:
    """Returns (brief fields, source) where source is 'llm' or 'fallback'.
    Never raises — see module docstring."""
    # The expander is the n8n webhook (the only provider exposing
    # complete_json); anthropic or missing config degrades to the fallback
    # brief — campaigns always create.
    if expander is None:
        if config.DRAFT_PROVIDER == "n8n" and config.N8N_LLM_URL:
            from .providers.n8n_llm import N8nDrafter
            expander = N8nDrafter()
        else:
            return fallback_brief(objective), "fallback"

    user = (
        f"Campaign objective:\n{objective}\n\n"
        f"Sender: {sender_name or 'unknown'}"
        f"{f', {sender_role}' if sender_role else ''}"
    )

    try:
        parsed = await expander.complete_json(SYSTEM_PROMPT, user)
    except Exception as exc:  # ProviderError, DraftRefused — never propagate
        log.warning("objective expansion failed, using fallback brief: %s", exc)
        return fallback_brief(objective), "fallback"

    # Empty string, never None, for the same NOT NULL reason as above.
    fields = {k: str(parsed.get(k) or "").strip() for k in EXPANSION_FIELDS}
    if not all(fields.get(k) for k in REQUIRED_FIELDS):
        log.warning("objective expansion returned an incomplete brief, "
                    "using fallback (keys: %s)", sorted(parsed))
        return fallback_brief(objective), "fallback"

    return fields, "llm"
