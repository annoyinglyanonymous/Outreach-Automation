from __future__ import annotations

import asyncio
import logging
import re

import httpx

from ..config import config
from .base import EnrichmentResult, ProviderError

log = logging.getLogger(__name__)

BULK_MATCH_URL = "https://api.apollo.io/api/v1/people/bulk_match"
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _norm_name(value: str | None) -> str:
    """Lowercase, strip punctuation and accents-as-typed, collapse spaces."""
    if not value:
        return ""
    return re.sub(r"[^a-z0-9 ]", "", value.lower()).strip()


def _norm_company(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"[^a-z0-9 ]", " ", value.lower())
    text = re.sub(
        r"\b(inc|llc|ltd|co|corp|corporation|company|group|holdings|"
        r"agency|agencies|insurance|the)\b",
        " ",
        text,
    )
    return re.sub(r"\s+", " ", text).strip()


def score_match(sent: dict, got: dict) -> float:
    score = 0.0

    sent_email = (sent.get("email") or "").strip().lower()
    got_emails = {
        (got.get("email") or "").strip().lower(),
        *[
            (e.get("email") or "").strip().lower()
            for e in (got.get("personal_emails") or [])
            if isinstance(e, dict)
        ],
    }
    got_emails.discard("")
    if sent_email and sent_email in got_emails:
        score += 0.45
    elif got.get("email_status") == "verified":
        score += 0.08

    sent_first, sent_last = _norm_name(sent.get("first_name")), _norm_name(sent.get("last_name"))
    got_first, got_last = _norm_name(got.get("first_name")), _norm_name(got.get("last_name"))
    if sent_first and sent_first == got_first:
        score += 0.12
    if sent_last and sent_last == got_last:
        score += 0.23

    sent_company = _norm_company(sent.get("organization_name"))
    got_org = got.get("organization") or {}
    got_company = _norm_company(got_org.get("name") if isinstance(got_org, dict) else None)
    if sent_company and got_company:
        if sent_company == got_company:
            score += 0.25
        elif sent_company in got_company or got_company in sent_company:
            score += 0.20

    return max(0.0, min(1.0, round(score, 2)))


class ApolloProvider:
    """Tier 1. Bulk lookup, chunked to Apollo's per-request cap."""

    name = "apollo"

    def __init__(self, api_key: str | None = None, batch_size: int | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.api_key = api_key or config.APOLLO_API_KEY
        # Apollo's bulk endpoint caps at 10 records per request and is
        # rate limited to half the single-match endpoint's per-minute
        # allowance, so chunks are small and paced.
        self.batch_size = batch_size or config.APOLLO_BATCH_SIZE
        self._transport = transport  # tests inject httpx.MockTransport

    # -- transport ---------------------------------------------------------

    async def _post_chunk(self, client: httpx.AsyncClient, details: list[dict]) -> list[dict]:
        last_error = "unknown"

        for attempt in range(config.PROVIDER_MAX_RETRIES):
            try:
                response = await client.post(
                    BULK_MATCH_URL,
                    headers={
                        "x-api-key": self.api_key,
                        "Content-Type": "application/json",
                        "Cache-Control": "no-cache",
                    },
                    json={"details": details},
                )
            except httpx.RequestError as exc:
                last_error = f"transport: {exc!s}"
            else:
                if response.status_code == 200:
                    # A proxy or WAF can answer 200 with an HTML error page.
                    # Letting json's ValueError escape would skip the
                    # runner's ProviderError handler, so the claimed batch
                    # would never be released.
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise ProviderError(
                            f"apollo: 200 with a non-JSON body ({exc})"
                        ) from exc
                    matches = payload.get("matches") if isinstance(payload, dict) else None
                    if not isinstance(matches, list):
                        raise ProviderError("apollo: response missing 'matches' array")
                    return matches

                if response.status_code not in RETRYABLE_STATUS:
                    # 401, 403, 422 and friends will fail identically on
                    # retry; surfacing immediately makes the cause obvious.
                    raise ProviderError(
                        f"apollo: {response.status_code} {response.text[:200]}"
                    )
                last_error = f"http {response.status_code}"

                # Honour Retry-After when the API tells us how long to wait.
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    await asyncio.sleep(min(int(retry_after), 60))
                    continue

            backoff = min(2**attempt, 30)
            log.warning(
                "apollo attempt %d/%d failed (%s), retrying in %ss",
                attempt + 1, config.PROVIDER_MAX_RETRIES, last_error, backoff,
            )
            await asyncio.sleep(backoff)

        raise ProviderError(
            f"apollo: gave up after {config.PROVIDER_MAX_RETRIES} attempts ({last_error})"
        )

    # -- public API --------------------------------------------------------

    async def enrich(self, contacts: list) -> list[EnrichmentResult]:
        """Look up every contact, returning one result each.

        Contacts Apollo could not match still come back, with
        linkedin_url=None. An explicit miss is unambiguous; a missing
        entry would leave the runner unable to tell "not found" from
        "never attempted".
        """
        if not contacts:
            return []

        results: list[EnrichmentResult] = []

        async with httpx.AsyncClient(
            timeout=config.PROVIDER_TIMEOUT_SECONDS, transport=self._transport
        ) as client:
            for start in range(0, len(contacts), self.batch_size):
                chunk = contacts[start : start + self.batch_size]

                details = [
                    {
                        "first_name": c.first_name,
                        "last_name": c.last_name or "",
                        "email": c.email,
                        "organization_name": c.company or "",
                        "title": c.title or "",
                    }
                    for c in chunk
                ]

                matches = await self._post_chunk(client, details)

                # Apollo returns matches positionally, nulls included for
                # misses. Zip defensively in case a short array comes back.
                for contact, sent, got in zip(
                    chunk, details, matches + [None] * (len(chunk) - len(matches))
                ):
                    if not isinstance(got, dict):
                        results.append(
                            EnrichmentResult(contact.email, None, 0.0, self.name)
                        )
                        continue

                    url = (got.get("linkedin_url") or "").strip() or None
                    confidence = score_match(sent, got) if url else 0.0

                    # A URL we cannot attribute confidently is worse than
                    # none: the drafter would write a personalised message
                    # to a stranger. Discard it and let the contact fall
                    # through to the template path.
                    if url and confidence < config.MIN_ACCEPT_CONFIDENCE:
                        log.info(
                            "apollo: discarding low-confidence match for %s (%.2f)",
                            contact.email, confidence,
                        )
                        url, confidence = None, 0.0

                    results.append(
                        EnrichmentResult(contact.email, url, confidence, self.name)
                    )

                # Space out chunks so a large batch does not trip the
                # per-minute limit and force the retry path.
                if start + self.batch_size < len(contacts):
                    await asyncio.sleep(config.APOLLO_CHUNK_DELAY_SECONDS)

        return results