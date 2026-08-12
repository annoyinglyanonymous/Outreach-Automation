"""Opted-in (transactional) email delivery via Resend. Only this module
speaks to it. Cold lists must never go through here — Resend's AUP
prohibits cold outreach; the claim query enforces the consent branch.

Idempotency: every send carries Idempotency-Key outreach/contact-{id}
(24h server-side window; verified against Resend docs, 2026-08). A retry
or crash-recovery replay returns the original email id instead of
sending twice. A 409 idempotency conflict means an email already went
out for this contact with different content — that is the double-send
protection firing, so it is treated as already-sent, not an error.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import config
from .base import ProviderError, SendRejected

log = logging.getLogger(__name__)

SEND_URL = "https://api.resend.com/emails"
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class ResendSender:
    name = "resend"

    def __init__(self, api_key: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.api_key = api_key or config.RESEND_API_KEY
        self._transport = transport  # tests inject httpx.MockTransport

    async def send(self, target) -> str:
        if not target.sender_email:
            raise SendRejected("campaign has no sender_email for transactional sends")

        sender = (
            f"{target.sender_name} <{target.sender_email}>"
            if target.sender_name else target.sender_email
        )
        payload = {
            "from": sender,
            "to": [target.email],
            "subject": target.email_subject,
            "text": target.email_body,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Idempotency-Key": f"outreach/contact-{target.id}",
        }

        last_error = "unknown"
        async with httpx.AsyncClient(
            timeout=config.PROVIDER_TIMEOUT_SECONDS, transport=self._transport
        ) as client:
            for attempt in range(config.PROVIDER_MAX_RETRIES):
                try:
                    response = await client.post(SEND_URL, headers=headers, json=payload)
                except httpx.RequestError as exc:
                    # Ambiguous: may have landed. Retrying is safe only
                    # because the idempotency key dedupes server-side.
                    last_error = f"transport: {exc!s}"
                else:
                    if response.status_code in (200, 201):
                        email_id = (response.json() or {}).get("id")
                        if not email_id:
                            raise ProviderError("resend: response missing email id")
                        return email_id
                    if response.status_code == 409:
                        log.warning(
                            "resend: idempotency conflict for contact %d — an email "
                            "already went out; treating as sent", target.id,
                        )
                        return "idempotent-conflict"
                    if response.status_code == 422:
                        raise SendRejected(f"resend rejected: {response.text[:200]}")
                    if response.status_code not in RETRYABLE_STATUS:
                        # 401/403 (bad key, unverified domain) fail every
                        # send identically — vendor-level, abort the run.
                        raise ProviderError(
                            f"resend: {response.status_code} {response.text[:200]}"
                        )
                    last_error = f"http {response.status_code}"

                    retry_after = response.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        await asyncio.sleep(min(int(retry_after), 60))
                        continue

                backoff = min(2**attempt, 30)
                log.warning(
                    "resend attempt %d/%d failed (%s), retrying in %ss",
                    attempt + 1, config.PROVIDER_MAX_RETRIES, last_error, backoff,
                )
                await asyncio.sleep(backoff)

        raise ProviderError(
            f"resend: gave up after {config.PROVIDER_MAX_RETRIES} attempts ({last_error})"
        )
