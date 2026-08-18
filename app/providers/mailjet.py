"""Cold (and any transactional) email delivery via Mailjet's Send API v3.1.
Only this module speaks to Mailjet.

Mailjet is a transactional ESP: unlike Smartlead it does not enrol a lead
into a warmed-mailbox campaign — it takes a fully rendered message and
sends it immediately. The drafted subject/body already live on the
EmailTarget, so this is mechanically the Resend provider with a different
auth (HTTP Basic over an api-key/secret pair) and body shape.

Idempotency — the one hard difference from Resend. Mailjet has NO
idempotency key. `CustomID` is only a correlation handle for the delivery
webhook, not a dedupe key, so the server will happily send twice if we
replay. We therefore split transport failures by whether the request could
have reached Mailjet:

- connection never established (ConnectError/ConnectTimeout/PoolTimeout):
  the message did not leave — safe to retry, then release on give-up.
- anything after that (read/write timeout, broken response): the message
  MAY have been accepted — raise SendUncertain so the runner leaves the
  contact at 'sending' for a human, never re-sending it.

This is what keeps invariant #1 (one first-touch email per contact, ever)
without provider-side idempotency.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import config
from .base import ProviderError, SendRejected, SendUncertain

log = logging.getLogger(__name__)

SEND_URL = "https://api.mailjet.com/v3.1/send"
SENDER_LIST_URL = "https://api.mailjet.com/v3/REST/sender"
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
# A short, fixed timeout for the UI sender-list read: a dead or slow
# Mailjet must not hang the campaign edit page for the full provider
# timeout (60s). The page degrades to manual entry if this raises.
SENDER_LIST_TIMEOUT = 10.0
# Transport failures that prove the request never reached Mailjet, so a
# retry (and, on give-up, a release) cannot double-send. Everything else
# that httpx raises is treated as ambiguous -> SendUncertain.
NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class MailjetSender:
    name = "mailjet"

    def __init__(self, api_key: str | None = None, secret_key: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.api_key = api_key or config.MAILJET_API_KEY
        self.secret_key = secret_key or config.MAILJET_SECRET_KEY
        self._transport = transport  # tests inject httpx.MockTransport

    async def list_verified_sender_records(self) -> list[dict]:
        """The verified sender records — ``{"email", "name"}`` — on the
        Mailjet account. Read-only and best-effort: callers (the sender
        dropdown, the pool auto-enrol sync) degrade rather than abort if
        this raises, so it must never break a page render or a send run.

        Only 'Active' (verified) individual addresses are returned.
        Wildcard domain senders ('*@domain.com', created when a whole
        domain is validated) are dropped — they authorise the domain but
        are not themselves a usable From address. Deduped by lower(email)
        and sorted so the dropdown order and the sync are deterministic.
        The display Name rides along so the pool can enrol a sender with
        the From name Mailjet already knows. A non-200, an unreachable
        Mailjet, or unset keys all raise ProviderError; there is no retry
        loop because a stale render/sync is not worth blocking on.
        """
        if not (self.api_key and self.secret_key):
            raise ProviderError("mailjet: API key/secret not configured")

        auth = httpx.BasicAuth(self.api_key, self.secret_key)
        async with httpx.AsyncClient(
            timeout=SENDER_LIST_TIMEOUT, transport=self._transport
        ) as client:
            try:
                response = await client.get(SENDER_LIST_URL, auth=auth)
            except httpx.RequestError as exc:
                raise ProviderError(f"mailjet: sender list unreachable ({exc!s})") from exc

        if response.status_code != 200:
            raise ProviderError(
                f"mailjet: sender list {response.status_code} {response.text[:200]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(f"mailjet: sender list non-JSON body ({exc})") from exc

        records: dict[str, dict] = {}
        for row in (data or {}).get("Data") or []:
            if str(row.get("Status", "")).lower() != "active":
                continue
            email = str(row.get("Email", "")).strip()
            if not email or email.startswith("*"):  # skip wildcard domain senders
                continue
            name = str(row.get("Name", "")).strip() or None
            records[email.lower()] = {"email": email, "name": name}
        return [records[key] for key in sorted(records)]

    async def list_verified_senders(self) -> list[str]:
        """The verified from-addresses only, for the sender dropdown's
        datalist. A thin projection of list_verified_sender_records."""
        return [r["email"] for r in await self.list_verified_sender_records()]

    async def send(self, target) -> str:
        if not target.sender_email:
            raise SendRejected("campaign has no sender_email for transactional sends")

        from_block: dict = {"Email": target.sender_email}
        if target.sender_name:
            from_block["Name"] = target.sender_name
        payload = {
            "Messages": [
                {
                    "From": from_block,
                    "To": [{"Email": target.email}],
                    "Subject": target.email_subject,
                    "TextPart": target.email_body,
                    # A correlation handle for the delivery webhook, NOT a
                    # dedupe key — Mailjet has none (see module docstring).
                    "CustomID": f"outreach-contact-{target.id}",
                }
            ]
        }
        auth = httpx.BasicAuth(self.api_key, self.secret_key)

        last_error = "unknown"
        async with httpx.AsyncClient(
            timeout=config.PROVIDER_TIMEOUT_SECONDS, transport=self._transport
        ) as client:
            for attempt in range(config.PROVIDER_MAX_RETRIES):
                try:
                    response = await client.post(SEND_URL, auth=auth, json=payload)
                except NOT_SENT_ERRORS as exc:
                    # The connection never opened — the message did not
                    # leave. Safe to retry, like Resend's transport retry.
                    last_error = f"connect: {exc!s}"
                except httpx.RequestError as exc:
                    # Read/write timeout or broken response: Mailjet may
                    # already have accepted it. No idempotency key means a
                    # retry or release could double-send, so hand it up as
                    # uncertain — the contact stays 'sending' for a human.
                    raise SendUncertain(
                        f"mailjet: ambiguous send for contact {target.id} ({exc!s})"
                    ) from exc
                else:
                    if response.status_code == 200:
                        return self._parse_success(response)
                    if response.status_code == 400:
                        # Per-message validation (bad recipient/sender) —
                        # an outcome for this one contact, not an outage.
                        raise SendRejected(f"mailjet rejected: {response.text[:200]}")
                    if response.status_code not in RETRYABLE_STATUS:
                        # 401/403 (bad keys, unverified sender) fail every
                        # send identically — vendor-level, abort the run.
                        raise ProviderError(
                            f"mailjet: {response.status_code} {response.text[:200]}"
                        )
                    last_error = f"http {response.status_code}"

                    retry_after = response.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        await asyncio.sleep(min(int(retry_after), 60))
                        continue

                backoff = min(2**attempt, 30)
                log.warning(
                    "mailjet attempt %d/%d failed (%s), retrying in %ss",
                    attempt + 1, config.PROVIDER_MAX_RETRIES, last_error, backoff,
                )
                await asyncio.sleep(backoff)

        raise ProviderError(
            f"mailjet: gave up after {config.PROVIDER_MAX_RETRIES} attempts ({last_error})"
        )

    @staticmethod
    def _parse_success(response: httpx.Response) -> str:
        """A 200 with the message accepted returns its MessageID (the audit
        ref). Mailjet can also return 200 with a per-message error status —
        that is a per-contact rejection, not a success."""
        body = response.json() or {}
        messages = body.get("Messages") or []
        first = messages[0] if messages else {}
        if str(first.get("Status", "")).lower() != "success":
            raise SendRejected(f"mailjet message not accepted: {response.text[:200]}")
        to = (first.get("To") or [{}])[0]
        message_id = to.get("MessageID") or to.get("MessageUUID")
        if not message_id:
            raise ProviderError("mailjet: response missing MessageID")
        return str(message_id)
