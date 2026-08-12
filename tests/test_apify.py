"""Apify transport — httpx.MockTransport, no network.

The collector's semantics are already covered in test_scraper.py against
a fake client; this file covers the layer underneath it, where a
misread vendor response turns into either a re-paid scrape or a run left
claimed forever.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import config
from app.providers import apify as apify_module
from app.providers.apify import ApifyClient
from app.providers.base import ProviderError


def client_with(handler, **kwargs) -> ApifyClient:
    return ApifyClient(token="test-token", actor_id="user/actor",
                       transport=httpx.MockTransport(handler), **kwargs)


# ---------------------------------------------------------------------
# start_run
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_run_posts_urls_under_the_configured_key():
    """The key carrying the URL array is config, so switching actors is
    an env change rather than a code change."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"data": {"id": "run-1"}})

    run_id = await client_with(handler).start_run(["https://linkedin.com/in/a"])

    assert run_id == "run-1"
    assert seen["auth"] == "Bearer test-token"
    # "user/actor" must be addressed as "user~actor" in the path.
    assert "/acts/user~actor/runs" in seen["url"]
    assert seen["body"] == {"profileUrls": ["https://linkedin.com/in/a"]}


@pytest.mark.asyncio
async def test_extra_input_is_merged_into_the_run_payload(monkeypatch):
    monkeypatch.setattr(type(config), "APIFY_EXTRA_INPUT",
                        '{"profileScraperMode": "no email"}')
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"data": {"id": "run-1"}})

    await client_with(handler).start_run(["https://linkedin.com/in/a"])

    assert seen["body"]["profileScraperMode"] == "no email"
    assert seen["body"]["profileUrls"] == ["https://linkedin.com/in/a"]


@pytest.mark.asyncio
async def test_malformed_extra_input_fails_before_any_request(monkeypatch):
    """A config mistake, but the caller must see the same "cannot start,
    release the batch" outcome as a vendor outage."""
    monkeypatch.setattr(type(config), "APIFY_EXTRA_INPUT", "not json")

    def handler(request):
        raise AssertionError("must not reach the network")

    with pytest.raises(ProviderError, match="APIFY_EXTRA_INPUT"):
        await client_with(handler).start_run(["https://linkedin.com/in/a"])


@pytest.mark.asyncio
async def test_start_run_without_an_id_is_a_provider_error():
    def handler(request):
        return httpx.Response(201, json={"data": {}})

    with pytest.raises(ProviderError, match="missing run id"):
        await client_with(handler).start_run(["https://linkedin.com/in/a"])


# ---------------------------------------------------------------------
# get_run
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_maps_status_and_dataset():
    def handler(request):
        return httpx.Response(200, json={
            "data": {"status": "SUCCEEDED", "defaultDatasetId": "ds-1"},
        })

    info = await client_with(handler).get_run("run-1")

    assert (info.status, info.dataset_id) == ("SUCCEEDED", "ds-1")
    assert info.succeeded is True
    assert info.in_flight is False


@pytest.mark.asyncio
async def test_expired_run_reports_as_missing_not_an_error():
    """Apify expires runs after its retention window. The results are gone
    for good, so this must surface as a failure the collector can release
    and re-scrape — not as an exception that leaves the run claimed."""
    def handler(request):
        return httpx.Response(404, json={"error": "not found"})

    info = await client_with(handler).get_run("run-1")

    assert info.status == "MISSING"
    assert info.in_flight is False


@pytest.mark.asyncio
async def test_unrecognised_status_stays_in_flight():
    """We never re-scrape (and re-pay) on a status we do not understand."""
    def handler(request):
        return httpx.Response(200, json={"data": {"status": "PAUSED"}})

    info = await client_with(handler).get_run("run-1")

    assert info.in_flight is True
    assert info.succeeded is False


# ---------------------------------------------------------------------
# fetch_items
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_items_pages_until_short_page(monkeypatch):
    monkeypatch.setattr(apify_module, "ITEMS_PAGE_SIZE", 2)
    offsets = []

    def handler(request):
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        pages = {0: [{"url": "a"}, {"url": "b"}],
                 2: [{"url": "c"}]}
        return httpx.Response(200, json=pages.get(offset, []))

    items = await client_with(handler).fetch_items("ds-1")

    assert offsets == [0, 2]
    assert [i["url"] for i in items] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_fetch_items_drops_non_dict_entries():
    def handler(request):
        return httpx.Response(200, json=[{"url": "a"}, "junk", None])

    items = await client_with(handler).fetch_items("ds-1")
    assert items == [{"url": "a"}]


@pytest.mark.asyncio
async def test_fetch_items_rejects_a_non_array_body():
    def handler(request):
        return httpx.Response(200, json={"data": []})

    with pytest.raises(ProviderError, match="expected a JSON array"):
        await client_with(handler).fetch_items("ds-1")


# ---------------------------------------------------------------------
# malformed responses — regression, fixed 2026-08-12
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_json_body_on_get_run_is_a_provider_error():
    """Used to escape as json.JSONDecodeError, past the scraper's
    ProviderError handler, leaving the run claimed with nothing released."""
    def handler(request):
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    with pytest.raises(ProviderError, match="non-JSON body"):
        await client_with(handler).get_run("run-1")


@pytest.mark.asyncio
async def test_non_json_body_on_fetch_items_is_a_provider_error():
    def handler(request):
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    with pytest.raises(ProviderError, match="non-JSON body"):
        await client_with(handler).fetch_items("ds-1")


@pytest.mark.asyncio
async def test_json_without_a_data_object_is_a_provider_error():
    def handler(request):
        return httpx.Response(200, json=["unexpected"])

    with pytest.raises(ProviderError, match="missing 'data' object"):
        await client_with(handler).get_run("run-1")


# ---------------------------------------------------------------------
# retry semantics
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_429_retries_then_succeeds(no_backoff):
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={})
        return httpx.Response(200, json={"data": {"status": "SUCCEEDED"}})

    info = await client_with(handler).get_run("run-1")

    assert info.status == "SUCCEEDED"
    assert no_backoff == [2]


@pytest.mark.asyncio
async def test_gives_up_after_max_retries(no_backoff, monkeypatch):
    monkeypatch.setattr(type(config), "PROVIDER_MAX_RETRIES", 2)

    def handler(request):
        return httpx.Response(503, json={})

    with pytest.raises(ProviderError, match="gave up"):
        await client_with(handler).get_run("run-1")
