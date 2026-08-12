"""Apollo transport + match scoring — httpx.MockTransport, no network.

Apollo returns no confidence score of its own, so one is derived from
agreement between what we sent and what came back. That derivation is
the only thing standing between the drafter and a personalised email to
a stranger, which makes the discard threshold the highest-stakes number
in the enrichment stage.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app import repo
from app.config import config
from app.providers.apollo import ApolloProvider, score_match
from app.providers.base import ProviderError


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------


def provider_with(handler, batch_size=10) -> ApolloProvider:
    return ApolloProvider(api_key="test-key", batch_size=batch_size,
                          transport=httpx.MockTransport(handler))


def matches(*people) -> httpx.Response:
    return httpx.Response(200, json={"matches": list(people)})


def contact(id=1, email="jane@doe-insurance.com", first="Jane", last="Doe",
            company="Doe Insurance") -> repo.Contact:
    return repo.Contact(id=id, email=email, first_name=first, last_name=last,
                        company=company, title="Agent")


def person(**overrides) -> dict:
    """An Apollo match that agrees with contact() on everything."""
    base = {
        "linkedin_url": "https://linkedin.com/in/jane-doe",
        "email": "jane@doe-insurance.com",
        "first_name": "Jane",
        "last_name": "Doe",
        "organization": {"name": "Doe Insurance"},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------


def test_full_agreement_scores_at_the_ceiling():
    sent = {"email": "jane@doe-insurance.com", "first_name": "Jane",
            "last_name": "Doe", "organization_name": "Doe Insurance"}
    assert score_match(sent, person()) == 1.0


def test_total_disagreement_scores_zero():
    sent = {"email": "jane@doe-insurance.com", "first_name": "Jane",
            "last_name": "Doe", "organization_name": "Doe Insurance"}
    got = person(email="bob@other.com", first_name="Robert",
                 last_name="Smith", organization={"name": "Acme Widgets"})
    assert score_match(sent, got) == 0.0


def test_company_normalisation_ignores_industry_boilerplate():
    """"Doe Insurance Agency, LLC" and "Doe" are the same firm; the
    source lists and Apollo rarely spell it the same way."""
    sent = {"organization_name": "Doe Insurance Agency, LLC"}
    got = {"organization": {"name": "Doe"}}
    assert score_match(sent, got) == 0.25


def test_surname_agreement_outweighs_forename():
    """A shared surname is far more identifying than a shared first name,
    which is why they are not weighted equally."""
    sent = {"first_name": "Jane", "last_name": "Doe"}
    assert score_match(sent, {"first_name": "Jane"}) < score_match(sent, {"last_name": "Doe"})


def test_personal_emails_array_counts_as_an_email_match():
    sent = {"email": "jane@personal.com"}
    got = {"email": "jane@work.com",
           "personal_emails": [{"email": "jane@personal.com"}]}
    assert score_match(sent, got) == 0.45


def test_score_is_clamped_to_the_unit_interval():
    sent = {"email": "jane@doe-insurance.com", "first_name": "Jane",
            "last_name": "Doe", "organization_name": "Doe Insurance"}
    # 0.45 + 0.12 + 0.23 + 0.25 = 1.05 before clamping.
    assert score_match(sent, person()) <= 1.0


# ---------------------------------------------------------------------
# the discard threshold — invariant: a URL we cannot attribute
# confidently is worse than no URL at all
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_confidence_match_is_discarded_not_written():
    """Apollo found *a* profile, but nothing about it agrees with the
    contact. Keeping it would personalise a cold email to a stranger."""
    def handler(request):
        return matches(person(email="bob@other.com", first_name="Robert",
                              last_name="Smith",
                              organization={"name": "Acme Widgets"}))

    (result,) = await provider_with(handler).enrich([contact()])

    assert result.linkedin_url is None
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_match_at_the_threshold_is_kept(monkeypatch):
    monkeypatch.setattr(type(config), "MIN_ACCEPT_CONFIDENCE", 0.45)

    def handler(request):
        # Email agreement alone: exactly 0.45.
        return matches(person(first_name="Robert", last_name="Smith",
                              organization={"name": "Acme Widgets"}))

    (result,) = await provider_with(handler).enrich([contact()])

    assert result.linkedin_url == "https://linkedin.com/in/jane-doe"
    assert result.confidence == 0.45


@pytest.mark.asyncio
async def test_an_exact_email_match_alone_is_below_the_default_threshold():
    """Characterisation, flagged deliberately: an exact email match scores
    0.45 against a default MIN_ACCEPT_CONFIDENCE of 0.55, so a contact
    whose name Apollo spells differently is discarded despite conclusive
    identity evidence. Locked in so the trade-off is a decision rather
    than an accident — if the weighting changes, this test should fail."""
    assert config.MIN_ACCEPT_CONFIDENCE == 0.55

    def handler(request):
        return matches(person(first_name="Robert", last_name="Smith",
                              organization={"name": "Acme Widgets"}))

    (result,) = await provider_with(handler).enrich([contact()])

    assert result.linkedin_url is None


# ---------------------------------------------------------------------
# the "explicit miss" contract
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_match_returns_an_explicit_miss():
    """A missing entry would leave the runner unable to tell "not found"
    from "never attempted"."""
    def handler(request):
        return matches(None)

    (result,) = await provider_with(handler).enrich([contact()])

    assert result.email == "jane@doe-insurance.com"
    assert result.linkedin_url is None
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_short_matches_array_still_yields_one_result_per_contact():
    """Apollo answers positionally; a truncated array must not silently
    drop the tail of the batch."""
    def handler(request):
        return matches(person())        # two sent, one returned

    results = await provider_with(handler).enrich(
        [contact(1), contact(2, email="bob@x.com")]
    )

    assert [r.email for r in results] == ["jane@doe-insurance.com", "bob@x.com"]
    assert results[1].linkedin_url is None


@pytest.mark.asyncio
async def test_blank_linkedin_url_is_a_miss():
    def handler(request):
        return matches(person(linkedin_url="   "))

    (result,) = await provider_with(handler).enrich([contact()])
    assert result.linkedin_url is None


# ---------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contacts_are_chunked_to_the_vendor_cap():
    """Apollo's bulk endpoint caps at 10 records per request."""
    sent_sizes = []

    def handler(request):
        body = json.loads(request.content)
        sent_sizes.append(len(body["details"]))
        return matches(*[None] * len(body["details"]))

    await provider_with(handler, batch_size=2).enrich(
        [contact(i, email=f"c{i}@x.com") for i in range(1, 6)]
    )

    assert sent_sizes == [2, 2, 1]


@pytest.mark.asyncio
async def test_no_contacts_makes_no_request():
    def handler(request):
        raise AssertionError("no HTTP call should be made for an empty batch")

    assert await provider_with(handler).enrich([]) == []


@pytest.mark.asyncio
async def test_request_carries_the_api_key_and_expected_fields():
    seen = {}

    def handler(request):
        seen["key"] = request.headers["x-api-key"]
        seen["details"] = json.loads(request.content)["details"]
        return matches(person())

    await provider_with(handler).enrich([contact()])

    assert seen["key"] == "test-key"
    assert seen["details"][0] == {
        "first_name": "Jane", "last_name": "Doe",
        "email": "jane@doe-insurance.com",
        "organization_name": "Doe Insurance", "title": "Agent",
    }


@pytest.mark.asyncio
async def test_missing_names_and_company_are_sent_as_empty_strings():
    """The contact table allows NULLs; Apollo's schema does not."""
    seen = {}

    def handler(request):
        seen["details"] = json.loads(request.content)["details"]
        return matches(None)

    bare = repo.Contact(id=1, email="a@x.com", first_name="Jane",
                        last_name=None, company=None, title=None)
    await provider_with(handler).enrich([bare])

    assert seen["details"][0]["last_name"] == ""
    assert seen["details"][0]["organization_name"] == ""
    assert seen["details"][0]["title"] == ""


# ---------------------------------------------------------------------
# transport failure semantics
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401_fails_immediately_without_retry(no_backoff):
    """A bad key fails identically on retry; surfacing it immediately
    makes the cause obvious instead of costing four timeouts first."""
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(401, json={"error": "unauthorized"})

    with pytest.raises(ProviderError, match="401"):
        await provider_with(handler).enrich([contact()])

    assert attempts["n"] == 1
    assert no_backoff == []


@pytest.mark.asyncio
async def test_429_retries_then_succeeds(no_backoff):
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={})
        return matches(person())

    (result,) = await provider_with(handler).enrich([contact()])

    assert result.linkedin_url == "https://linkedin.com/in/jane-doe"
    assert attempts["n"] == 2
    assert no_backoff == [3]        # Retry-After honoured, not the backoff


@pytest.mark.asyncio
async def test_gives_up_after_max_retries(no_backoff, monkeypatch):
    monkeypatch.setattr(type(config), "PROVIDER_MAX_RETRIES", 3)
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(503, json={})

    with pytest.raises(ProviderError, match="gave up"):
        await provider_with(handler).enrich([contact()])

    assert attempts["n"] == 3
    assert no_backoff == [1, 2, 4]  # exponential; the last one is wasted


@pytest.mark.asyncio
async def test_transport_error_is_retried_then_surfaced(no_backoff, monkeypatch):
    monkeypatch.setattr(type(config), "PROVIDER_MAX_RETRIES", 2)

    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ProviderError, match="transport"):
        await provider_with(handler).enrich([contact()])


@pytest.mark.asyncio
async def test_missing_matches_array_is_a_provider_error():
    """A 200 whose shape we do not recognise must release the batch, not
    be read as "nobody matched"."""
    def handler(request):
        return httpx.Response(200, json={"people": []})

    with pytest.raises(ProviderError, match="missing 'matches'"):
        await provider_with(handler).enrich([contact()])


@pytest.mark.asyncio
async def test_non_json_body_is_a_provider_error():
    """A WAF or proxy answering 200 with an HTML error page used to escape
    as json.JSONDecodeError. The runner catches only ProviderError, so the
    claimed batch was never released and sat at 'enriching' until the
    15-minute stale sweep. Fixed 2026-08-12."""
    def handler(request):
        return httpx.Response(200, text="<html>503 Service Unavailable</html>")

    with pytest.raises(ProviderError, match="non-JSON body"):
        await provider_with(handler).enrich([contact()])


@pytest.mark.asyncio
async def test_json_array_body_is_a_provider_error():
    """Valid JSON of the wrong shape must not reach .get() either."""
    def handler(request):
        return httpx.Response(200, json=[])

    with pytest.raises(ProviderError, match="missing 'matches'"):
        await provider_with(handler).enrich([contact()])
