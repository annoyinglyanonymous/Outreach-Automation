"""AI enrichment verification: judge whether the LinkedIn profile a vendor
matched (Apollo) and scraped (Apify) really belongs to the intended
contact. A wrong match means personalising a cold email to a stranger.

No HTTP here — it reuses the drafting LLM route (`complete_json`) through
the same provider gate as `campaign_brief.expand_objective`. Conservative
by contract: anything short of a clear same-person match is `unsure`,
which the runner treats as a rejection (the contact falls back to the
template / email-only path). A wrong personalised email is worse than a
generic one.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from .config import config

log = logging.getLogger(__name__)

VERDICTS = ("right_person", "wrong_person", "unsure")

SYSTEM_PROMPT = (
    "You verify whether a LinkedIn profile belongs to a specific person we "
    "are about to cold-email. You are given the person we intend to reach "
    "(from a purchased contact list) and the LinkedIn profile a vendor "
    "matched to them. Decide whether they are the same individual.\n\n"
    "Judge on name, current employer/company, and role. Answer "
    "'right_person' only when the identity clearly lines up — the same "
    "name at the same or a clearly related company, or an unmistakable "
    "name-and-role match. A common name alone, a different company, or a "
    "role that plainly does not fit is NOT a match. When the evidence is "
    "thin or conflicting, answer 'unsure' rather than guessing — a wrong "
    "match is worse than none.\n\n"
    "Respond with a single JSON object of exactly this form: "
    '{"verdict": "right_person" | "wrong_person" | "unsure", '
    '"confidence": number from 0 to 1, "reason": short string}. '
    "No other keys, no markdown fences."
)


@dataclass
class Verdict:
    verdict: str            # one of VERDICTS
    confidence: float
    reason: str

    @property
    def is_match(self) -> bool:
        return self.verdict == "right_person"

    def as_reason(self) -> str:
        """One-line audit note stored on the verdict event."""
        return f"AI: {self.verdict} ({self.confidence:.2f}) — {self.reason}".strip()[:400]


def build_verify_prompt(target: "object") -> tuple[str, str]:
    """(system, user) for one match. `target` is a repo.VerifyTarget."""
    profile = json.dumps(target.profile_data or {}, ensure_ascii=False)
    if len(profile) > config.DRAFT_PROFILE_CHAR_LIMIT:
        # Same truncation rationale as drafting: the useful signal
        # (headline, current role, recent experience) serialises first.
        profile = profile[: config.DRAFT_PROFILE_CHAR_LIMIT]
    user = (
        "Intended contact (from our list):\n"
        f"- Name: {target.first_name} {target.last_name or ''}\n"
        f"- Company: {target.company or 'unknown'}\n"
        f"- Title: {target.title or 'unknown'}\n"
        f"- Email: {target.email}\n\n"
        f"Matched LinkedIn URL: {target.linkedin_url or 'unknown'}\n"
        f"Scraped LinkedIn profile (JSON, may be truncated):\n{profile}"
    )
    return SYSTEM_PROMPT, user


def parse_verdict(parsed: dict) -> Verdict:
    """Map a raw LLM object to a Verdict. Anything unrecognised collapses
    to 'unsure' — the safe (reject) side."""
    verdict = str(parsed.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        verdict = "unsure"
    try:
        confidence = max(0.0, min(1.0, float(parsed.get("confidence"))))
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(parsed.get("reason") or "").strip()[:300]
    return Verdict(verdict, confidence, reason)


def build_verifier():
    """The LLM provider exposing `complete_json`, chosen exactly as
    `campaign_brief.expand_objective` does. None when none is configured —
    the stage then no-ops and the manual verify page remains the path.
    (Anthropic has no `complete_json`, so it is not an option here.)"""
    if config.DRAFT_PROVIDER == "n8n" and config.N8N_LLM_URL:
        from .providers.n8n_llm import N8nDrafter
        return N8nDrafter()
    if config.DRAFT_PROVIDER == "groq" and config.GROQ_API_KEY:
        from .providers.groq import GroqDrafter
        return GroqDrafter()
    return None


async def verify_match(target: "object", verifier) -> Verdict:
    """One verification call. A vendor outage propagates as ProviderError
    (the runner stops and retries next tick); a well-formed-but-odd
    response is coerced to 'unsure' by parse_verdict."""
    system, user = build_verify_prompt(target)
    parsed = await verifier.complete_json(system, user)
    return parse_verdict(parsed)
