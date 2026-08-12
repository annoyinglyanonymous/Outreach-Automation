"""Cold-email delivery via Smartlead. Only this module speaks to it.

A "send" here is pushing one lead into the campaign's Smartlead
campaign, whose sequence is a bare shell of the merge variables
{{personalized_subject}} / {{personalized_body}} — so Smartlead's warmed
mailboxes deliver OUR pre-personalised copy on its own schedule.

Idempotency: Smartlead skips a lead already present in the same
campaign and reports it in the skipped counts (verified against its API
docs, 2026-08). A re-push after an ambiguous failure therefore cannot
enqueue the sequence twice — the skip is treated as success.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from ..config import config
from .base import ProviderError, SendRejected

log = logging.getLogger(__name__)

API_BASE = "https://server.smartlead.ai/api/v1"
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# Activation, as documented (POST /campaigns/{id}/status, enum
# PAUSED/STOPPED/START). Constants so a doc drift is a one-line fix.
ACTIVATE_METHOD = "POST"
ACTIVATE_PAYLOAD = {"status": "START"}

# The shell sequence campaign creation installs: our drafts arrive as
# per-lead custom fields, so the sequence is nothing but the merge
# variables. pre-wrap keeps the plain-text body's newlines when
# Smartlead renders the custom field into HTML.
SHELL_SEQUENCE = {
    "sequences": [{
        "id": None,
        "seq_number": 1,
        "subject": "{{personalized_subject}}",
        "email_body": '<p style="white-space: pre-wrap">{{personalized_body}}</p>',
        "seq_delay_details": {"delay_in_days": 0},
    }],
}


@dataclass
class CampaignSetup:
    """Outcome of the auto-setup chain. failed_step is None on full
    success; otherwise the id exists but that step needs finishing in
    the Smartlead UI."""
    campaign_id: str
    failed_step: str | None = None
    error: str | None = None

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
        log.info(
    "smartlead send: contact_id=%s email=%s campaign_id=%s",
    target.id,
    target.email,
    target.smartlead_campaign_id,
)

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

    # -- campaign auto-setup -------------------------------------------------

    async def setup_campaign(self, name: str) -> CampaignSetup:
        """Create and fully configure a Smartlead campaign: shell
        sequence, every connected mailbox, default schedule, activate.

        Stops at the first failure: activating a half-configured campaign
        is worse than leaving it visible-but-drafted. Steps after create
        report the failed step instead of raising — the id is the money
        field and the caller persists it regardless. Two attempts per
        call, not the send path's four: six sequential calls sit inside a
        browser request.
        """
        data = await self._request(
            "POST", f"{API_BASE}/campaigns/create", {"name": name}, max_attempts=2
        )
        campaign_id = str(data.get("id") or "")
        if not campaign_id:
            raise ProviderError(f"smartlead: create returned no id: {str(data)[:200]}")

        async def step(label: str, coro) -> str | None:
            try:
                await coro
                return None
            except ProviderError as exc:
                log.error("smartlead setup: %s failed for campaign %s: %s",
                          label, campaign_id, exc)
                return str(exc)

        error = await step("sequence", self._request(
            "POST", f"{API_BASE}/campaigns/{campaign_id}/sequences",
            SHELL_SEQUENCE, max_attempts=2))
        if error:
            return CampaignSetup(campaign_id, "sequence", error)

        try:
            accounts = await self._request(
                "GET", f"{API_BASE}/email-accounts/", None, max_attempts=2)
        except ProviderError as exc:
            return CampaignSetup(campaign_id, "email-accounts", str(exc))
        account_ids = [a["id"] for a in accounts or [] if isinstance(a, dict) and a.get("id")]
        if not account_ids:
            return CampaignSetup(campaign_id, "email-accounts",
                                 "no connected email accounts on the Smartlead account")

        error = await step("email-accounts", self._request(
            "POST", f"{API_BASE}/campaigns/{campaign_id}/email-accounts",
            {"email_account_ids": account_ids}, max_attempts=2))
        if error:
            return CampaignSetup(campaign_id, "email-accounts", error)

        error = await step("schedule", self._request(
            "POST", f"{API_BASE}/campaigns/{campaign_id}/schedule", {
                "timezone": config.SMARTLEAD_SCHEDULE_TIMEZONE,
                "days_of_the_week": list(config.SMARTLEAD_SCHEDULE_DAYS),
                "start_hour": config.SMARTLEAD_SCHEDULE_START_HOUR,
                "end_hour": config.SMARTLEAD_SCHEDULE_END_HOUR,
                "min_time_btw_emails": config.SMARTLEAD_MIN_TIME_BTW_EMAILS,
                "max_new_leads_per_day": config.SMARTLEAD_MAX_NEW_LEADS_PER_DAY,
            }, max_attempts=2))
        if error:
            return CampaignSetup(campaign_id, "schedule", error)

        error = await step("activate", self._request(
            ACTIVATE_METHOD, f"{API_BASE}/campaigns/{campaign_id}/status",
            ACTIVATE_PAYLOAD, max_attempts=2))
        if error:
            return CampaignSetup(campaign_id, "activate", error)

        return CampaignSetup(campaign_id)

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
                    # are safe to retry because Smartlead dedupes them;
                    # setup calls are idempotent-enough (re-saving a
                    # sequence or schedule overwrites) except create,
                    # whose duplicate would be an inert empty campaign.
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


async def setup_campaign(name: str) -> CampaignSetup:
    """Routes import this module-level wrapper (like n8n.ingest) so tests
    monkeypatch one obvious seam."""
    return await SmartleadSender().setup_campaign(name)
