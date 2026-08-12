"""Stage 3 vendor transport (Groq). Only this module speaks to Groq.

OpenAI-compatible chat completions with JSON mode. Raw httpx with the
same retry shape as apollo.py — pulling in an SDK for one endpoint is
not worth the dependency.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx

from ..config import config
from .base import Draft, DraftRefused, ProviderError

log = logging.getLogger(__name__)

CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# JSON mode guarantees syntactically valid JSON but not the shape, so
# the shape is pinned in the prompt and validated on parse. (JSON mode
# also requires the word "JSON" to appear in the messages.)
FORMAT_INSTRUCTION = (
    "\n\nRespond with a single JSON object of exactly this form: "
    '{"subject": string, "body": string, "linkedin_note": string}. '
    "No other keys, no markdown fences."
)


def _reasoning_effort() -> str:
    # Groq's gpt-oss models accept low/medium/high only; DRAFT_EFFORT
    # may hold Anthropic-only tiers like "xhigh" if the provider was
    # switched without retuning.
    effort = config.DRAFT_EFFORT
    return effort if effort in ("low", "medium", "high") else "high"


class GroqDrafter:
    name = "groq"

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.api_key = api_key or config.GROQ_API_KEY
        self.model = model or config.DRAFT_MODEL or "openai/gpt-oss-120b"
        self._transport = transport  # tests inject httpx.MockTransport

    async def draft(self, system: str, user: str) -> Draft:
        parsed = await self.complete_json(system + FORMAT_INSTRUCTION, user)
        return Draft(
            subject=(parsed.get("subject") or "").strip(),
            body=(parsed.get("body") or "").strip(),
            linkedin_note=(parsed.get("linkedin_note") or "").strip() or None,
        )

    async def complete_json(self, system: str, user: str,
                            max_tokens: int | None = None) -> dict:
        """One JSON-mode completion, returned as the parsed object. The
        caller pins its own output shape inside `system` — JSON mode only
        guarantees syntax, never keys."""
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens or config.DRAFT_MAX_TOKENS,
        }
        if self.model.startswith("openai/gpt-oss"):
            # Only the gpt-oss family takes this knob; other models 400 on
            # unknown parameters.
            payload["reasoning_effort"] = _reasoning_effort()

        data = await self._post(payload)

        choice = (data.get("choices") or [{}])[0]
        finish = choice.get("finish_reason")
        if finish == "content_filter":
            raise DraftRefused("model declined this request")
        if finish == "length":
            raise ProviderError("groq: output truncated — raise DRAFT_MAX_TOKENS")

        content = (choice.get("message") or {}).get("content") or ""
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"groq: response was not valid JSON: {content[:120]}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ProviderError("groq: response JSON was not an object")
        return parsed

    # -- transport ---------------------------------------------------------

    async def _post(self, payload: dict) -> dict:
        last_error = "unknown"

        async with httpx.AsyncClient(
            timeout=config.PROVIDER_TIMEOUT_SECONDS, transport=self._transport
        ) as client:
            for attempt in range(config.PROVIDER_MAX_RETRIES):
                try:
                    response = await client.post(
                        CHAT_URL,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=payload,
                    )
                except httpx.RequestError as exc:
                    last_error = f"transport: {exc!s}"
                else:
                    if response.status_code == 200:
                        # A proxy or WAF can answer 200 with an HTML error
                        # page; json's ValueError would escape past the
                        # drafting runner's ProviderError handler and abort
                        # the run instead of releasing the contact.
                        try:
                            data = response.json()
                        except ValueError as exc:
                            raise ProviderError(
                                f"groq: 200 with a non-JSON body ({exc})"
                            ) from exc
                        if not isinstance(data, dict):
                            raise ProviderError("groq: response was not a JSON object")
                        return data
                    if response.status_code not in RETRYABLE_STATUS:
                        # 401/404/422 fail identically on retry; surfacing
                        # immediately makes the cause obvious.
                        raise ProviderError(
                            f"groq: {response.status_code} {response.text[:200]}"
                        )
                    last_error = f"http {response.status_code}"

                    retry_after = response.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        await asyncio.sleep(min(int(retry_after), 60))
                        continue

                backoff = min(2**attempt, 30)
                log.warning(
                    "groq attempt %d/%d failed (%s), retrying in %ss",
                    attempt + 1, config.PROVIDER_MAX_RETRIES, last_error, backoff,
                )
                await asyncio.sleep(backoff)

        raise ProviderError(
            f"groq: gave up after {config.PROVIDER_MAX_RETRIES} attempts ({last_error})"
        )
