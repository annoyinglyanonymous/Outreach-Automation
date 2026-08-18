"""Cold-email delivery via Smartlead — retained as a CUTOVER FALLBACK only.

Cold now sends via Mailjet (transactional); this module is used only when
the Mailjet pair is unset and SMARTLEAD_API_KEY is, and only for legacy
campaigns that still carry a smartlead_campaign_id. The campaign-setup
machinery (create/sequence/mailboxes/schedule/activate) was removed with
the UI that drove it; what remains is the lead-push send path.

A "send" here is pushing one lead into the campaign's Smartlead campaign,
whose sequence is a bare shell of the merge variables
{{personalized_subject}} / {{personalized_body}} — so Smartlead's warmed
mailboxes deliver OUR pre-personalised copy on its own schedule.

Idempotency: Smartlead skips a lead already present in the same campaign
and reports it in the skipped counts (verified against its API docs,
2026-08). A re-push after an ambiguous failure therefore cannot enqueue
the sequence twice — the skip is treated as success.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import config
from .base import ProviderError, SendRejected

log = logging.getLogger(__name__)

API_BASE = "https://server.smartlead.ai/api/v1"
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# Smartlead response field names have drifted across API versions; read
# every known spelling so a rename cannot silently misclassify a send.
ADDED_KEYS = ("upload_count", "added_count")
DUPLICATE_KEYS = ("already_added_to_campaign", "duplicate_count", "skipped_count")
INVALID_KEYS = ("invalid_email_count", "invalid_emails_count")


def _count(data: dict, keys: tuple[str, ...]) -> int:
    total = 0
    for key in keys:
        try:
            total += int(data.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return total


class SmartleadSender:
    name = "smartlead"

    def __init__(self, api_key: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.api_key = api_key or config.SMARTLEAD_API_KEY
        self._transport = transport  # tests inject httpx.MockTransport

    async def send(self, target) -> str:
        payload = {
            "lead_list": [{
                "email": target.email,
                "first_name": target.first_name or "",
                "last_name": target.last_name or "",
                "company_name": target.company or "",
                "custom_fields": {
                    "personalized_subject": target.email_subject,
                    "personalized_body": target.email_body,
                },
            }],
            # Deliberately NOT ignoring Smartlead's block/unsubscribe
            # lists: it is the compliance layer for cold sends.
            "settings": {
                "ignore_global_block_list": False,
                "ignore_unsubscribe_list": False,
                "ignore_duplicate_leads_in_other_campaign": False,
            },
        }
        log.info("smartlead send: contact_id=%s email=%s campaign_id=%s",
                 target.id, target.email, target.smartlead_campaign_id)

        data = await self._post(
            f"{API_BASE}/campaigns/{target.smartlead_campaign_id}/leads", payload
        )

        if _count(data, ADDED_KEYS) >= 1:
            return "lead-added"
        if _count(data, DUPLICATE_KEYS) >= 1:
            # Already in the campaign (idempotent replay after a crash or
            # retry, or blocked/unsubscribed by Smartlead's own lists).
            log.info("smartlead: %s already in campaign %s, treating as sent",
                     target.email, target.smartlead_campaign_id)
            return "duplicate-skip"
        if _count(data, INVALID_KEYS) >= 1:
            raise SendRejected("smartlead rejected the email address as invalid")
        raise ProviderError(f"smartlead: unrecognised response: {str(data)[:200]}")

    # -- transport ---------------------------------------------------------

    async def _post(self, url: str, payload: dict) -> dict:
        return await self._request("POST", url, payload)

    async def _request(self, method: str, url: str, payload: dict | None,
                       max_attempts: int | None = None):
        attempts = max_attempts or config.PROVIDER_MAX_RETRIES
        last_error = "unknown"

        async with httpx.AsyncClient(
            timeout=config.PROVIDER_TIMEOUT_SECONDS, transport=self._transport
        ) as client:
            for attempt in range(attempts):
                try:
                    response = await client.request(
                        method, url, params={"api_key": self.api_key},
                        json=payload,
                    )
                except httpx.RequestError as exc:
                    # Ambiguous: the request may have landed. Lead pushes
                    # are safe to retry because Smartlead dedupes them.
                    last_error = f"transport: {exc!s}"
                else:
                    if response.status_code == 200:
                        try:
                            return response.json()
                        except ValueError as exc:
                            raise ProviderError(
                                f"smartlead: non-JSON response: {response.text[:200]}"
                            ) from exc
                    if response.status_code not in RETRYABLE_STATUS:
                        raise ProviderError(
                            f"smartlead: {response.status_code} {response.text[:200]}"
                        )
                    last_error = f"http {response.status_code}"

                    retry_after = response.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        await asyncio.sleep(min(int(retry_after), 60))
                        continue

                backoff = min(2**attempt, 30)
                log.warning(
                    "smartlead attempt %d/%d failed (%s), retrying in %ss",
                    attempt + 1, attempts, last_error, backoff,
                )
                await asyncio.sleep(backoff)

        raise ProviderError(
            f"smartlead: gave up after {attempts} attempts ({last_error})"
        )
