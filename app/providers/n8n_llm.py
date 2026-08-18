"""LLM completions via an n8n webhook fronting the org's OpenAI
credential (docs/n8n-llm-workflow.json). Only this module speaks to it.

Contract: POST {"system": ..., "user": ...} → the model's JSON object,
verbatim. The workflow owns the vendor and model choice (OpenAI
gpt-4o-mini today) — changing models is an n8n node edit, not a deploy.

Retries are 2 attempts on transport/5xx only: a duplicated LLM call is
a fraction of a cent, and the drafting runner's release-and-retry
semantics cover anything that still fails.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import config
from .base import FORMAT_INSTRUCTION, Draft, ProviderError

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class N8nDrafter:
    name = "n8n"

    def __init__(self, url: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.url = url or config.N8N_LLM_URL
        self._transport = transport  # tests inject httpx.MockTransport

    async def draft(self, system: str, user: str) -> Draft:
        parsed = await self.complete_json(system + FORMAT_INSTRUCTION, user)
        draft = Draft(
            subject=(parsed.get("subject") or "").strip(),
            body=(parsed.get("body") or "").strip(),
            linkedin_note=(parsed.get("linkedin_note") or "").strip() or None,
        )
        if not draft.subject or not draft.body:
            # Empty output is a provider fault, not a refusal: release
            # and retry rather than failing the contact.
            raise ProviderError(
                f"n8n llm: draft missing subject/body: {str(parsed)[:200]}")
        return draft

    async def complete_json(self, system: str, user: str,
                            max_tokens: int | None = None) -> dict:
        """max_tokens is accepted for interface parity with the Drafter
        protocol but ignored — the n8n workflow owns generation limits."""
        if not self.url:
            raise ProviderError("n8n llm: N8N_LLM_URL is not configured")

        last_error = "unknown"
        async with httpx.AsyncClient(
            timeout=config.PROVIDER_TIMEOUT_SECONDS, transport=self._transport
        ) as client:
            for attempt in range(MAX_ATTEMPTS):
                try:
                    response = await client.post(
                        self.url, json={"system": system, "user": user})
                except httpx.RequestError as exc:
                    last_error = f"transport: {exc!s}"
                else:
                    if response.status_code == 200:
                        try:
                            parsed = response.json()
                        except ValueError as exc:
                            raise ProviderError(
                                f"n8n llm: non-JSON response: {response.text[:200]}"
                            ) from exc
                        if not isinstance(parsed, dict):
                            raise ProviderError(
                                f"n8n llm: expected a JSON object, got: "
                                f"{str(parsed)[:200]}")
                        if parsed.get("error"):
                            # The workflow's own failure shape (e.g. the
                            # model emitted unparseable content).
                            raise ProviderError(f"n8n llm: {parsed['error']}")
                        return parsed
                    if response.status_code not in RETRYABLE_STATUS:
                        # A 404 here usually means the workflow is not
                        # Active — same trap as the ingest webhook.
                        raise ProviderError(
                            f"n8n llm: {response.status_code} {response.text[:200]}")
                    last_error = f"http {response.status_code}"

                if attempt + 1 < MAX_ATTEMPTS:
                    backoff = min(2 ** attempt, 10)
                    log.warning("n8n llm attempt %d/%d failed (%s), retrying in %ss",
                                attempt + 1, MAX_ATTEMPTS, last_error, backoff)
                    await asyncio.sleep(backoff)

        raise ProviderError(
            f"n8n llm: gave up after {MAX_ATTEMPTS} attempts ({last_error})")
