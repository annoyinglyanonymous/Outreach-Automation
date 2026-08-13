"""Quick-create flow tests — every vendor is faked at the routes' import
sites. They lock in the prime rule: campaign creation never fails because
a vendor failed, and every outcome rides the redirect as a whitelisted
enum."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import repo
from app.config import config
from app.providers.base import ProviderError
from app.providers.smartlead import CampaignSetup
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
    monkeypatch.setattr(type(config), "SMARTLEAD_API_KEY", "test-key")

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
        "setup": CampaignSetup("4417"),
        "campaign": dict(CAMPAIGN),
        "created": None,
        "id_written": None,
        "ingested": None,
        "setup_calls": 0,
        "setup_mailboxes": "unset",
        "mailboxes_set": None,
        "accounts": [{"id": 11, "email": "a@x.com"}, {"id": 12, "email": "b@x.com"}],
    }

    async def expand_objective(objective, sender_name, sender_role, expander=None):
        s["objective_args"] = (objective, sender_name, sender_role)
        return s["brief"]

    async def setup_campaign(name, mailboxes=None):
        s["setup_calls"] += 1
        s["setup_mailboxes"] = mailboxes
        result = s["setup"]
        if isinstance(result, Exception):
            raise result
        return result

    async def set_campaign_mailboxes(cid, emails):
        s["mailboxes_set"] = (cid, emails)
        return True

    async def list_email_accounts():
        return s["accounts"]

    async def create_campaign(fields):
        s["created"] = dict(fields)
        return 1

    async def set_smartlead_campaign_id(cid, sl_id):
        s["id_written"] = (cid, sl_id)
        return True

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
    monkeypatch.setattr(routes.smartlead, "setup_campaign", setup_campaign)
    monkeypatch.setattr(routes.smartlead, "list_email_accounts", list_email_accounts)
    monkeypatch.setattr(routes.n8n, "ingest", ingest)
    monkeypatch.setattr(repo, "create_campaign", create_campaign)
    monkeypatch.setattr(repo, "set_smartlead_campaign_id", set_smartlead_campaign_id)
    monkeypatch.setattr(repo, "set_campaign_mailboxes", set_campaign_mailboxes)
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
    assert "smartlead=ok" in location
    assert "ingested=2" in location

    assert state["created"]["consent_status"] == "cold"
    assert state["created"]["status"] == "active"
    assert state["created"]["offer_description"] == "Generated offer"
    assert state["created"]["sender_name"] == "Dana"
    # Live-schema compliance (constraints verified 2026-08-10): the
    # CHECK'd enum gets a legal value; NOT NULL text columns never None;
    # delivery columns keep their load-bearing NULLs.
    assert state["created"]["channel_policy"] == "email_only"
    assert state["created"]["sender_email"] is None
    assert state["created"]["smartlead_campaign_id"] is None
    assert not any(v is None for k, v in state["created"].items()
                   if k not in ("sender_email", "smartlead_campaign_id"))
    assert state["id_written"] == (1, "4417")
    assert state["ingested"] == (1, 2)
    assert state["objective_args"] == ("Sell the platform", "Dana", "AE")
    assert state["nudges"] == ["enrich"]  # ingested contacts start enriching now


def test_fallback_brief_flagged(client, session_cookie, state):
    state["brief"] = ({**BRIEF, "offer_description": "Sell the platform"}, "fallback")
    response = create(client, session_cookie)
    assert "brief=fallback" in response.headers["location"]
    assert state["created"]["offer_description"] == "Sell the platform"


def test_smartlead_failure_keeps_campaign(client, session_cookie, state):
    state["setup"] = ProviderError("smartlead: down")
    response = create(client, session_cookie)
    assert response.status_code == 303
    assert "smartlead=failed" in response.headers["location"]
    assert state["created"] is not None       # campaign still created
    assert state["id_written"] is None        # no id to write
    assert state["ingested"] == (1, 2)        # CSV still ingested


def test_partial_setup_writes_id(client, session_cookie, state):
    state["setup"] = CampaignSetup("4417", failed_step="schedule", error="400")
    response = create(client, session_cookie)
    assert "smartlead=partial-schedule" in response.headers["location"]
    assert state["id_written"] == (1, "4417")


def test_unconfigured_key(client, session_cookie, state, monkeypatch):
    monkeypatch.setattr(type(config), "SMARTLEAD_API_KEY", "")
    response = create(client, session_cookie)
    assert "smartlead=unconfigured" in response.headers["location"]
    assert state["setup_calls"] == 0


def test_ingest_failure_keeps_everything_else(client, session_cookie, state):
    state["ingest_error"] = ProviderError("n8n: down")
    response = create(client, session_cookie)
    location = response.headers["location"]
    assert "smartlead=ok" in location
    assert "ingest=failed" in location
    assert "ingested=" not in location.replace("ingest=failed", "")
    assert state["nudges"] == []  # nothing landed, nothing to enrich


def test_no_csv_never_calls_ingest(client, session_cookie, state):
    response = create(client, session_cookie, with_csv=False)
    assert response.status_code == 303
    assert state["ingested"] is None
    assert "ingest" not in response.headers["location"]


# ---- mailbox selection ------------------------------------------------


def test_create_with_mailboxes_persists_and_threads_to_setup(client, session_cookie, state):
    create(client, session_cookie, with_csv=False,
           mailboxes=["a@x.com", "b@x.com"])
    # Persisted on the campaign AND passed to the Smartlead attach.
    assert state["mailboxes_set"] == (1, ["a@x.com", "b@x.com"])
    assert state["setup_mailboxes"] == ["a@x.com", "b@x.com"]


def test_create_without_mailboxes_defaults_to_all(client, session_cookie, state):
    create(client, session_cookie, with_csv=False)
    # None ticked -> stored NULL and setup gets None (attach all).
    assert state["mailboxes_set"] == (1, None)
    assert state["setup_mailboxes"] is None


def test_mailbox_picker_fragment_lists_connected(client, session_cookie, state):
    response = client.get("/ui/fragments/mailboxes", cookies=session_cookie)
    assert response.status_code == 200
    body = response.text
    assert 'value="a@x.com"' in body and 'value="b@x.com"' in body
    assert "checked" not in body            # new form: nothing pre-selected


def test_mailbox_picker_fragment_preselects_stored(client, session_cookie, state):
    state["campaign"]["smartlead_mailboxes"] = ["a@x.com"]
    body = client.get("/ui/fragments/mailboxes?campaign_id=1", cookies=session_cookie).text
    assert body.count("checked") == 1       # only the stored one is ticked
    # a@x.com is checked; b@x.com is not.
    assert 'value="a@x.com"' in body and 'value="b@x.com"' in body


def test_mailbox_picker_degrades_without_api_key(client, session_cookie, state, monkeypatch):
    monkeypatch.setattr(type(config), "SMARTLEAD_API_KEY", "")
    body = client.get("/ui/fragments/mailboxes", cookies=session_cookie).text
    # Jinja autoescapes the apostrophe in "isn't", so match an escape-free part.
    assert "use all mailboxes" in body
    assert 'type="checkbox"' not in body


def test_save_mailboxes_persists(client, session_cookie, state):
    response = client.post("/ui/campaigns/1/mailboxes", cookies=session_cookie,
                           data={"mailboxes": ["a@x.com"]})
    assert response.status_code == 200
    assert state["mailboxes_set"] == (1, ["a@x.com"])
    assert "Saved" in response.text


def test_save_mailboxes_empty_stores_null(client, session_cookie, state):
    client.post("/ui/campaigns/1/mailboxes", cookies=session_cookie, data={})
    assert state["mailboxes_set"] == (1, None)   # empty selection -> all


def test_set_campaign_mailboxes_sql_targets_the_column():
    """Fakes bypass SQL, so pin the constant (as done for other repo SQL)."""
    sql = repo.SET_CAMPAIGN_MAILBOXES_SQL
    assert "UPDATE campaigns" in sql and "smartlead_mailboxes" in sql


def test_missing_objective_is_422_with_no_side_effects(client, session_cookie, state):
    response = create(client, session_cookie, objective="")
    assert response.status_code == 422
    assert state["created"] is None
    assert state["setup_calls"] == 0


def test_retry_runs_when_id_missing(client, session_cookie, state):
    response = client.post("/ui/campaigns/1/smartlead-setup", cookies=session_cookie)
    assert response.status_code == 303
    assert "smartlead=ok" in response.headers["location"]
    assert state["id_written"] == (1, "4417")


def test_retry_skips_when_id_present(client, session_cookie, state):
    state["campaign"]["smartlead_campaign_id"] = "9999"
    response = client.post("/ui/campaigns/1/smartlead-setup", cookies=session_cookie)
    assert "smartlead=exists" in response.headers["location"]
    assert state["setup_calls"] == 0


def test_retry_unknown_campaign_404(client, session_cookie, state):
    state["campaign"] = None
    response = client.post("/ui/campaigns/7/smartlead-setup", cookies=session_cookie)
    assert response.status_code == 404


def test_delete_removes_campaign_and_redirects(client, session_cookie, state):
    response = client.post("/ui/campaigns/1/delete", cookies=session_cookie)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/campaigns"
    assert state["deleted"] == 1


def test_delete_edit_page_shows_danger_zone(client, session_cookie, state):
    state["campaign"]["smartlead_campaign_id"] = "3788171"
    response = client.get("/ui/campaigns/1", cookies=session_cookie)
    assert response.status_code == 200
    assert "Danger zone" in response.text
    assert "/ui/campaigns/1/delete" in response.text
    # The vendor campaign id is surfaced so the reviewer knows what is NOT deleted.
    assert "3788171" in response.text


def test_edit_page_decodes_flashes(client, session_cookie, state):
    response = client.get(
        "/ui/campaigns/1?brief=llm&smartlead=partial-schedule&ingested=2",
        cookies=session_cookie)
    assert response.status_code == 200
    assert "brief generated" in response.text.lower()
    assert "schedule" in response.text
    assert "2 contact(s) sent to ingestion" in response.text


def test_edit_page_ignores_unknown_flash_values(client, session_cookie, state):
    # Non-whitelisted flash enums render nothing, so an attacker-controlled
    # value never reaches the DOM. Probe with a distinctive sentinel rather
    # than a bare "<script>": the page carries a legitimate <script> poller
    # (the review-queue badge), so we assert the injected value is absent,
    # not that the page is script-free.
    response = client.get(
        "/ui/campaigns/1?brief=<script>xss-probe</script>&smartlead=nope&ingested=xx",
        cookies=session_cookie)
    assert response.status_code == 200
    assert "xss-probe" not in response.text
    assert "flash warn" not in response.text and "flash ok" not in response.text
