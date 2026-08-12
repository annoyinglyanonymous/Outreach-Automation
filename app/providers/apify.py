"""Stage 2 vendor transport. Only this module speaks to Apify.

Apify runs are asynchronous on the vendor side: start a run, poll its
status, then read the default dataset. There is deliberately no
waitForFinish — the scraper's collector reconciles finished runs on a
later trigger, so nothing ever blocks a batch.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

import httpx

from ..config import config
from .base import ProviderError

log = logging.getLogger(__name__)

API_BASE = "https://api.apify.com/v2"
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
ITEMS_PAGE_SIZE = 1000

# Run states Apify documents as still moving. Anything terminal that is
# not SUCCEEDED counts as failed; anything unrecognised is treated as
# in-flight so we never re-scrape (and re-pay) on a status we do not
# understand — it stays visible in /stats until a human looks.
IN_FLIGHT_STATUSES = {"READY", "RUNNING", "TIMING-OUT", "ABORTING"}


@dataclass(slots=True)
class RunInfo:
    run_id: str
    status: str
    dataset_id: str | None

    @property
    def succeeded(self) -> bool:
        return self.status == "SUCCEEDED"

    @property
    def in_flight(self) -> bool:
        if self.status in IN_FLIGHT_STATUSES:
            return True
        if self.status not in {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED", "MISSING"}:
            log.warning("apify run %s: unrecognised status %r, leaving in flight",
                        self.run_id, self.status)
            return True
        return False


class ApifyClient:
    name = "apify"

    def __init__(self, token: str | None = None, actor_id: str | None = None):
        self.token = token or config.APIFY_TOKEN
        # "user/actor" ids must be addressed as "user~actor" in URL paths.
        self.actor_id = (actor_id or config.APIFY_ACTOR_ID).replace("/", "~")

    # -- transport ---------------------------------------------------------

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Retry transient failures; hand every other response back to the
        caller, which knows which non-200s are meaningful (e.g. 404 on a
        run that Apify has expired)."""
        last_error = "unknown"

        async with httpx.AsyncClient(timeout=config.PROVIDER_TIMEOUT_SECONDS) as client:
            for attempt in range(config.PROVIDER_MAX_RETRIES):
                try:
                    response = await client.request(
                        method,
                        url,
                        headers={"Authorization": f"Bearer {self.token}"},
                        **kwargs,
                    )
                except httpx.RequestError as exc:
                    last_error = f"transport: {exc!s}"
                else:
                    if response.status_code not in RETRYABLE_STATUS:
                        return response
                    last_error = f"http {response.status_code}"

                    retry_after = response.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        await asyncio.sleep(min(int(retry_after), 60))
                        continue

                backoff = min(2**attempt, 30)
                log.warning(
                    "apify attempt %d/%d failed (%s), retrying in %ss",
                    attempt + 1, config.PROVIDER_MAX_RETRIES, last_error, backoff,
                )
                await asyncio.sleep(backoff)

        raise ProviderError(
            f"apify: gave up after {config.PROVIDER_MAX_RETRIES} attempts ({last_error})"
        )

    @staticmethod
    def _data_or_raise(response: httpx.Response, context: str) -> dict:
        if response.status_code not in (200, 201):
            raise ProviderError(
                f"apify {context}: {response.status_code} {response.text[:200]}"
            )
        data = response.json().get("data")
        if not isinstance(data, dict):
            raise ProviderError(f"apify {context}: response missing 'data' object")
        return data

    # -- public API ----------------------------------------------------------

    async def start_run(self, urls: list[str]) -> str:
        payload: dict = {config.APIFY_INPUT_KEY: urls}
        if config.APIFY_EXTRA_INPUT:
            try:
                payload.update(json.loads(config.APIFY_EXTRA_INPUT))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                # A config mistake, not a vendor outage — but the effect is
                # the same for the caller: the run cannot start, release.
                raise ProviderError(f"APIFY_EXTRA_INPUT is not a JSON object: {exc}")

        response = await self._request(
            "POST", f"{API_BASE}/acts/{self.actor_id}/runs", json=payload
        )
        data = self._data_or_raise(response, "start_run")
        run_id = data.get("id")
        if not run_id:
            raise ProviderError("apify start_run: response missing run id")
        return run_id

    async def get_run(self, run_id: str) -> RunInfo:
        response = await self._request("GET", f"{API_BASE}/actor-runs/{run_id}")
        if response.status_code == 404:
            # Apify expires runs after its retention window. If we still
            # hold contacts against one, the results are gone for good —
            # report it as a failure so they get re-scraped.
            return RunInfo(run_id=run_id, status="MISSING", dataset_id=None)
        data = self._data_or_raise(response, "get_run")
        return RunInfo(
            run_id=run_id,
            status=data.get("status", ""),
            dataset_id=data.get("defaultDatasetId"),
        )

    async def fetch_items(self, dataset_id: str) -> list[dict]:
        items: list[dict] = []
        offset = 0
        while True:
            response = await self._request(
                "GET",
                f"{API_BASE}/datasets/{dataset_id}/items",
                params={
                    "format": "json",
                    "clean": "true",
                    "offset": offset,
                    "limit": ITEMS_PAGE_SIZE,
                },
            )
            if response.status_code != 200:
                raise ProviderError(
                    f"apify fetch_items: {response.status_code} {response.text[:200]}"
                )
            page = response.json()
            if not isinstance(page, list):
                raise ProviderError("apify fetch_items: expected a JSON array")
            items.extend(i for i in page if isinstance(i, dict))
            if len(page) < ITEMS_PAGE_SIZE:
                return items
            offset += ITEMS_PAGE_SIZE
