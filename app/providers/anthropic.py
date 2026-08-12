"""Stage 3 vendor transport. Only this module speaks to the Claude API.

Uses the official SDK rather than raw httpx: it ships typed errors and
automatic retry/backoff for 429/5xx — everything apollo.py had to
hand-roll against a vendor with no SDK worth using.
"""
from __future__ import annotations

import json
import logging

import anthropic

from ..config import config
from .base import Draft, DraftRefused, ProviderError

log = logging.getLogger(__name__)

# Structured output: the API guarantees the response parses as this
# shape, so there is no "find the JSON inside prose" step to go wrong.
# The schema subset cannot enforce string length, so the 300-char
# LinkedIn note limit is enforced by the runner instead.
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {
            "type": "string",
            "description": "Email subject line, under 60 characters, no clickbait",
        },
        "body": {
            "type": "string",
            "description": "Email body, plain text, under 120 words",
        },
        "linkedin_note": {
            "type": "string",
            "description": "LinkedIn connection note, under 260 characters, no links",
        },
    },
    "required": ["subject", "body", "linkedin_note"],
    "additionalProperties": False,
}


class AnthropicDrafter:
    name = "anthropic"

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or config.ANTHROPIC_API_KEY,
            max_retries=config.PROVIDER_MAX_RETRIES,
            timeout=float(config.PROVIDER_TIMEOUT_SECONDS),
        )
        self.model = model or config.DRAFT_MODEL or "claude-opus-4-8"

    async def draft(self, system: str, user: str) -> Draft:
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=config.DRAFT_MAX_TOKENS,
                system=system,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": config.DRAFT_EFFORT,
                    "format": {"type": "json_schema", "schema": DRAFT_SCHEMA},
                },
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"anthropic: connection failed: {exc}") from exc
        except anthropic.APIStatusError as exc:
            # The SDK already retried 429/5xx with backoff; anything that
            # reaches here is exhausted retries or a non-retryable request
            # error. Both mean "stop the run", same as the other stages.
            raise ProviderError(f"anthropic: {exc.status_code} {exc.message}") from exc

        # Check the stop reason before touching content: a refusal can
        # arrive with empty content, and max_tokens means a half-draft
        # that must never be sent to a prospect.
        if response.stop_reason == "refusal":
            raise DraftRefused("model declined to draft this contact")
        if response.stop_reason == "max_tokens":
            raise ProviderError(
                "anthropic: draft truncated at max_tokens — raise DRAFT_MAX_TOKENS"
            )

        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"anthropic: response was not valid JSON: {text[:120]}") from exc

        return Draft(
            subject=(data.get("subject") or "").strip(),
            body=(data.get("body") or "").strip(),
            linkedin_note=(data.get("linkedin_note") or "").strip() or None,
        )
