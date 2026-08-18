"""Quick-create flow tests — every vendor is faked at the routes' import
sites. They lock in the prime rule: campaign creation never fails because
a vendor failed, and every outcome rides the redirect as a whitelisted
enum. Cold sending is now Mailjet (transactional), so there is no
campaign/mailbox setup step at create time — a cold campaign just needs a
verified sender_email set on the edit page before it can send."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import repo
from app.config import config
from app.providers.base import ProviderError
from app.ui import router, routes
from app.ui.auth import COOKIE_NAME, sign_session

CSV = b"email,first_name,company\njane@x.com,Jane,Acme\nbob@y.com,Bob,Beta\n"

BRIEF = {
    "offer_description": "Generated offer",
    "cta": "Call?",
    "tone": "direct",
    "audience_rationale": "Agencies",
    "fallback_email_subject": "Hi {{company}}",
    "fallback_email_body": "Hi {{first_name}}\n{{sender}}",
}

CAMPAIGN = {
    "id": 1, "name": "Test", "status": "active", "consent_status": "cold",
    "smartlead_campaign_id": None, "sender_email": None,
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(type(config), "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(type(config), "SESSION_MAX_AGE_MINUTES", 480)

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def session_cookie(client):
    return {COOKIE_NAME: sign_session("tester@example.com")}


@pytest.fixture
def state(monkeypatch):
    s = {
        "brief": (dict(BRIEF), "llm"),
        "campaign": dict(CAMPAIGN),
        "created": None,
        "ingested": None,
    }

    async def expand_objective(objective, sender_name, sender_role, expander=None):
        s["objective_args"] = (objective, sender_name, sender_role)
        return s["brief"]

    async def create_campaign(fields):
        s["created"] = dict(fields)
        return 1

    async def get_campaign(cid):
        return dict(s["campaign"]) if s["campaign"] else None

    async def delete_campaign(cid):
        s["deleted"] = cid
        return s["campaign"] is not None

    async def ingest(cid, rows):
        if isinstance(s.get("ingest_error"), Exception):
            raise s["ingest_error"]
        s["ingested"] = (cid, len(rows))
        return {"ok": True}

    monkeypatch.setattr(routes.campaign_brief, "expand_objective", expand_objective)
    monkeypatch.setattr(routes.n8n, "ingest", ingest)
    monkeypatch.setattr(repo, "create_campaign", create_campaign)
    monkeypatch.setattr(repo, "get_campaign", get_campaign)
    monkeypatch.setattr(repo, "delete_campaign", delete_campaign)

    s["nudges"] = []
    monkeypatch.setattr(routes.runs, "nudge",
                        lambda stage: s["nudges"].append(stage) or True)
    return s


def create(client, session_cookie, with_csv=True, **overrides):
    data = {"name": "Test", "objective": "Sell the platform",
            "sender_name": "Dana", "sender_role": "AE"}
    data.update(overrides)
    files = {"file": ("c.csv", CSV, "text/csv")} if with_csv else None
    return client.post("/ui/campaigns", data=data, files=files, cookies=session_cookie)


def test_happy_path(client, session_cookie, state):
    response = create(client, session_cookie)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/ui/campaigns/1?")
    assert "brief=llm" in location
    assert "ingested=2" in location
    # No vendor setup at create time any more — no smartlead flash.
    assert "smartlead=" not in location

    assert state["created"]["consent_status"] == "cold"
    assert state["created"]["status"] == "active"
    assert state["created"]["offer_description"] == "Generated offer"
    assert state["created"]["sender_name"] == "Dana"
    # Live-schema compliance (constraints verified 2026-08-10): the
    # CHECK'd enum gets a legal value; NOT NULL text columns never None;
    # delivery columns keep their load-bearing NULLs (a cold campaign is
    # unsendable until sender_email is set on the edit page).
    assert state["created"]["channel_policy"] == "email_only"
    assert state["created"]["sender_email"] is None
    assert state["created"]["smartlead_campaign_id"] is None
    assert not any(v is None for k, v in state["created"].items()
                   if k not in ("sender_email", "smartlead_campaign_id"))
    assert state["ingested"] == (1, 2)
    assert state["objective_args"] == ("Sell the platform", "Dana", "AE")
    assert state["nudges"] == ["enrich"]  # ingested contacts start enriching now


def test_fallback_brief_flagged(client, session_cookie, state):
    state["brief"] = ({**BRIEF, "offer_description": "Sell the platform"}, "fallback")
    response = create(client, session_cookie)
    assert "brief=fallback" in response.headers["location"]
    assert state["created"]["offer_description"] == "Sell the platform"


def test_ingest_failure_keeps_everything_else(client, session_cookie, state):
    state["ingest_error"] = ProviderError("n8n: down")
    response = create(client, session_cookie)
    location = response.headers["location"]
    assert "brief=llm" in location
    assert "ingest=failed" in location
    assert "ingested=" not in location.replace("ingest=failed", "")
    assert state["created"] is not None    # campaign still created
    assert state["nudges"] == []           # nothing landed, nothing to enrich


def test_no_csv_never_calls_ingest(client, session_cookie, state):
    response = create(client, session_cookie, with_csv=False)
    assert response.status_code == 303
    assert state["ingested"] is None
    assert "ingest" not in response.headers["location"]


# ---- preview email ----------------------------------------------------


def test_preview_route_renders_sample_and_remaps_brief(client, session_cookie, monkeypatch):
    """The route feeds the drafter the aliased brief (offer_description ->
    offer, sender_name -> sender) and renders the returned sample."""
    captured = {}

    async def fake_preview(campaign, drafter=None):
        captured["campaign"] = campaign
        return {
            "from": {"name": "Rushel", "role": "AI Associate"},
            "sample": {"name": "Jordan Reyes", "title": "Owner",
                       "company": "Reyes Insurance Group", "email": "jordan@reyes.com"},
            "template": {"subject": "Quick question", "body": "Hi there"},
            "personalized": {"subject": "Explore new markets",
                             "body": "Hi Jordan, ...", "linkedin_note": "Nice to connect"},
            "error": None,
        }

    monkeypatch.setattr(routes.drafting, "preview_draft", fake_preview)

    response = client.post("/ui/campaigns/1/preview", cookies=session_cookie, data={
        "offer_description": "Agency Height markets",
        "sender_name": "Rushel",
        "tone": "friendly",
        "cta": "quick call?",
        "fallback_email_subject": "New markets",
        "fallback_email_body": "Hi {{first_name}}",
    })
    assert response.status_code == 200
    body = response.text
    assert "Explore new markets" in body            # personalized subject rendered
    assert "nothing is saved or sent" in body       # the sample-only hint
    # edit-form field names arrive remapped to the drafter's aliased keys
    assert captured["campaign"]["offer"] == "Agency Height markets"
    assert captured["campaign"]["sender"] == "Rushel"
    assert captured["campaign"]["tone"] == "friendly"


def test_preview_route_rejects_cross_origin(client, session_cookie, monkeypatch):
    called = {"n": 0}

    async def fake_preview(campaign, drafter=None):
        called["n"] += 1
        return {"sample": {}, "template": {}, "personalized": None, "error": None}

    monkeypatch.setattr(routes.drafting, "preview_draft", fake_preview)

    response = client.post("/ui/campaigns/1/preview", cookies=session_cookie,
                           headers={"Origin": "https://evil.example"},
                           data={"offer_description": "x"})
    assert response.status_code == 403
    assert called["n"] == 0                          # blocked before any drafting


def test_missing_objective_is_422_with_no_side_effects(client, session_cookie, state):
    response = create(client, session_cookie, objective="")
    assert response.status_code == 422
    assert state["created"] is None


def test_delete_removes_campaign_and_redirects(client, session_cookie, state):
    response = client.post("/ui/campaigns/1/delete", cookies=session_cookie)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/campaigns"
    assert state["deleted"] == 1


def test_delete_edit_page_shows_danger_zone(client, session_cookie, state):
    response = client.get("/ui/campaigns/1", cookies=session_cookie)
    assert response.status_code == 200
    assert "Danger zone" in response.text
    assert "/ui/campaigns/1/delete" in response.text


def test_cold_campaign_without_sender_email_shows_not_ready(client, session_cookie, state):
    """A cold campaign can't send until a verified Mailjet sender_email is
    set — the edit page surfaces that rather than leaving it a mystery."""
    response = client.get("/ui/campaigns/1", cookies=session_cookie)
    assert response.status_code == 200
    assert "Not ready to send" in response.text


def test_edit_page_decodes_flashes(client, session_cookie, state):
    response = client.get(
        "/ui/campaigns/1?brief=llm&ingested=2",
        cookies=session_cookie)
    assert response.status_code == 200
    assert "brief generated" in response.text.lower()
    assert "2 contact(s) sent to ingestion" in response.text


def test_edit_page_ignores_unknown_flash_values(client, session_cookie, state):
    # Non-whitelisted flash enums render nothing, so an attacker-controlled
    # value never reaches the DOM. Probe with a distinctive sentinel rather
    # than a bare "<script>": the page carries a legitimate <script> poller
    # (the review-queue badge), so we assert the injected value is absent,
    # not that the page is script-free.
    response = client.get(
        "/ui/campaigns/1?brief=<script>xss-probe</script>&ingested=xx",
        cookies=session_cookie)
    assert response.status_code == 200
    assert "xss-probe" not in response.text
    assert "flash warn" not in response.text and "flash ok" not in response.text
