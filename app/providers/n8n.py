"""CSV-upload ingestion proxy. Only this module speaks to the n8n webhook.

Ingestion stays canonical in n8n (atomic insert, in-batch dedupe,
suppression check); duplicating that logic here would mean two
implementations to keep in sync. An n8n outage therefore breaks CSV
upload only — the pipeline itself never depends on it.
"""
from __future__ import annotations

import logging

import httpx

from ..config import config
from .base import ProviderError

log = logging.getLogger(__name__)


async def ingest(campaign_id: int, contacts: list[dict]) -> dict:
    if not config.N8N_INGEST_URL:
        raise ProviderError("N8N_INGEST_URL is not configured")

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.post(
                config.N8N_INGEST_URL,
                json={"campaign_id": campaign_id, "contacts": contacts},
            )
        except httpx.RequestError as exc:
            raise ProviderError(f"n8n ingest: transport: {exc}") from exc

    if response.status_code >= 300:
        raise ProviderError(
            f"n8n ingest: {response.status_code} {response.text[:200]}"
        )
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text[:500]}
