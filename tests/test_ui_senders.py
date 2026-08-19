"""Sender-pool admin UI tests — fakes only. The pool is the cold From
rotation set (migration 010); these lock the CRUD, the active toggle, and
the verified-address helper degrading to manual entry when Mailjet is
unreachable."""
from __future__ import annotations

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import repo
from app.config import config
from app.ui import router, routes
from app.ui.auth import COOKIE_NAME, sign_session

SENDER = {"id": 1, "sender_email": "john@d1.com", "sender_name": "John",
          "active": True, "daily_cap": 25, "sent_today": 4}


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
    s = {"senders": [dict(SENDER)], "sender": dict(SENDER), "verified": ([], None),
         "created": None, "updated": None, "toggled": None, "deleted": None,
         "dup": False}

    async def list_senders():
        return s["senders"]

    async def create_sender(fields):
        if s["dup"]:
            raise asyncpg.UniqueViolationError("dup")
        s["created"] = dict(fields)
        return 2

    async def get_sender(sender_id):
        return dict(s["sender"]) if s["sender"] else None

    async def update_sender(sender_id, fields):
        if s["dup"]:
            raise asyncpg.UniqueViolationError("dup")
        s["updated"] = (sender_id, dict(fields))
        return True

    async def set_sender_active(sender_id, active):
        s["toggled"] = (sender_id, active)
        return True

    async def delete_sender(sender_id):
        s["deleted"] = sender_id
        return True

    async def _verified_senders():
        return s["verified"]

    monkeypatch.setattr(repo, "list_senders", list_senders)
    monkeypatch.setattr(repo, "create_sender", create_sender)
    monkeypatch.setattr(repo, "get_sender", get_sender)
    monkeypatch.setattr(repo, "update_sender", update_sender)
    monkeypatch.setattr(repo, "set_sender_active", set_sender_active)
    monkeypatch.setattr(repo, "delete_sender", delete_sender)
    monkeypatch.setattr(routes, "_verified_senders", _verified_senders)
    return s


def test_list_renders_the_pool(client, session_cookie, state):
    body = client.get("/ui/senders", cookies=session_cookie).text
    assert "john@d1.com" in body
    assert "Add sender" in body


def test_list_shows_no_cap_for_zero(client, session_cookie, state):
    """daily_cap 0 = unlimited (the drip is the throttle): the page shows ∞,
    not a bare 0 that would read as 'this mailbox can't send'."""
    state["senders"] = [dict(SENDER, daily_cap=0)]
    body = client.get("/ui/senders", cookies=session_cookie).text
    assert "∞ (no cap)" in body


def test_empty_pool_shows_prompt(client, session_cookie, state):
    state["senders"] = []
    body = client.get("/ui/senders", cookies=session_cookie).text
    assert "No senders yet" in body


def test_page_shows_sync_banner_when_pool_changed(client, session_cookie, state, monkeypatch):
    """Loading the page auto-enrols from Mailjet; when that changed the pool,
    the counts are surfaced so the operator sees what synced."""
    async def fake_sync():
        return {"inserted": 2, "deactivated": 1, "error": None}
    monkeypatch.setattr(routes.emailer, "sync_pool", fake_sync)
    body = client.get("/ui/senders", cookies=session_cookie).text
    assert "Synced from Mailjet: 2 enrolled, 1 paused" in body


def test_page_warns_when_mailjet_sync_fails(client, session_cookie, state, monkeypatch):
    """A Mailjet outage during the load-time sync degrades to a warning — the
    last-synced pool still renders, never a 500 or a silently-wiped pool."""
    async def fake_sync():
        return {"inserted": 0, "deactivated": 0,
                "error": "mailjet: sender list unreachable"}
    monkeypatch.setattr(routes.emailer, "sync_pool", fake_sync)
    body = client.get("/ui/senders", cookies=session_cookie).text
    assert "Couldn't sync from Mailjet" in body


def test_new_page_offers_verified_addresses(client, session_cookie, state):
    state["verified"] = (["a@dom.com", "b@dom.com"], None)
    body = client.get("/ui/senders/new", cookies=session_cookie).text
    assert '<datalist id="verified-senders">' in body
    assert '<option value="a@dom.com">' in body


def test_new_page_degrades_when_mailjet_unavailable(client, session_cookie, state):
    state["verified"] = ([], "mailjet: keys not configured")
    body = client.get("/ui/senders/new", cookies=session_cookie).text
    assert "Couldn't load verified senders" in body
    assert '<input name="sender_email"' in body


def test_create_persists_and_redirects(client, session_cookie, state):
    r = client.post("/ui/senders", cookies=session_cookie,
                    data={"sender_email": "new@d2.com", "sender_name": "New",
                          "daily_cap": "30", "active": "on"})
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/senders"
    assert state["created"] == {"sender_email": "new@d2.com", "sender_name": "New",
                                "daily_cap": 30, "active": True, "signature": None}


def test_create_requires_an_email(client, session_cookie, state):
    r = client.post("/ui/senders", cookies=session_cookie, data={"daily_cap": "25"})
    assert r.status_code == 422
    assert state["created"] is None


def test_create_duplicate_is_reported(client, session_cookie, state):
    state["dup"] = True
    r = client.post("/ui/senders", cookies=session_cookie,
                    data={"sender_email": "john@d1.com"})
    assert r.status_code == 422
    assert "already in the pool" in r.text


def test_create_defaults_cap_when_blank(client, session_cookie, state):
    client.post("/ui/senders", cookies=session_cookie,
                data={"sender_email": "x@d.com", "daily_cap": ""})
    assert state["created"]["daily_cap"] == config.MAILJET_SENDER_DAILY_CAP


def test_unchecked_active_creates_a_paused_sender(client, session_cookie, state):
    client.post("/ui/senders", cookies=session_cookie,
                data={"sender_email": "x@d.com"})   # no 'active' field posted
    assert state["created"]["active"] is False


def test_edit_renders_existing(client, session_cookie, state):
    body = client.get("/ui/senders/1", cookies=session_cookie).text
    assert "john@d1.com" in body
    assert "Delete sender" in body


def test_edit_missing_is_404(client, session_cookie, state):
    state["sender"] = None
    assert client.get("/ui/senders/9", cookies=session_cookie).status_code == 404


def test_update_persists_and_redirects(client, session_cookie, state):
    r = client.post("/ui/senders/1", cookies=session_cookie,
                    data={"sender_email": "john@d1.com", "sender_name": "Johnny",
                          "daily_cap": "50", "active": "on"})
    assert r.status_code == 303
    assert state["updated"] == (1, {"sender_email": "john@d1.com",
                                    "sender_name": "Johnny",
                                    "daily_cap": 50, "active": True,
                                    "signature": None})


def test_update_requires_an_email(client, session_cookie, state):
    r = client.post("/ui/senders/1", cookies=session_cookie, data={"daily_cap": "10"})
    assert r.status_code == 422
    assert state["updated"] is None


def test_toggle_flips_active(client, session_cookie, state):
    r = client.post("/ui/senders/1/toggle", cookies=session_cookie)
    assert r.status_code == 303
    assert state["toggled"] == (1, False)   # SENDER is active -> toggled off


def test_toggle_missing_is_404(client, session_cookie, state):
    state["sender"] = None
    assert client.post("/ui/senders/9/toggle", cookies=session_cookie).status_code == 404


def test_delete_removes_and_redirects(client, session_cookie, state):
    r = client.post("/ui/senders/1/delete", cookies=session_cookie)
    assert r.status_code == 303
    assert state["deleted"] == 1


# ---- address allowlist (migration 013) -------------------------------

ALLOW = ("business@renegadeinsurance.info", "aayush.gupta@renegade-insurance.com")


def test_create_rejects_a_non_allowlisted_address(client, session_cookie, state, monkeypatch):
    monkeypatch.setattr(type(config), "SENDER_ALLOWED_ADDRESSES", ALLOW)
    r = client.post("/ui/senders", cookies=session_cookie,
                    data={"sender_email": "someone@gmail.com"})
    assert r.status_code == 422
    assert "not allowed" in r.text.lower()
    assert state["created"] is None


def test_update_rejects_a_non_allowlisted_address(client, session_cookie, state, monkeypatch):
    monkeypatch.setattr(type(config), "SENDER_ALLOWED_ADDRESSES", ALLOW)
    r = client.post("/ui/senders/1", cookies=session_cookie,
                    data={"sender_email": "someone@gmail.com"})
    assert r.status_code == 422
    assert state["updated"] is None


def test_create_allows_an_allowlisted_address(client, session_cookie, state, monkeypatch):
    monkeypatch.setattr(type(config), "SENDER_ALLOWED_ADDRESSES", ALLOW)
    r = client.post("/ui/senders", cookies=session_cookie,
                    data={"sender_email": "business@renegadeinsurance.info"})
    assert r.status_code == 303
    assert state["created"]["sender_email"] == "business@renegadeinsurance.info"


def test_create_rejects_another_address_on_an_allowed_domain(client, session_cookie, state, monkeypatch):
    """The allowlist is exact addresses, not domains — a different mailbox on
    an approved domain is still rejected."""
    monkeypatch.setattr(type(config), "SENDER_ALLOWED_ADDRESSES", ALLOW)
    r = client.post("/ui/senders", cookies=session_cookie,
                    data={"sender_email": "random@renegadeinsurance.info"})
    assert r.status_code == 422
    assert state["created"] is None


def test_toggle_will_not_activate_a_non_allowlisted_sender(client, session_cookie, state, monkeypatch):
    monkeypatch.setattr(type(config), "SENDER_ALLOWED_ADDRESSES", ALLOW)
    state["sender"] = {**SENDER, "sender_email": "old@gmail.com", "active": False}
    r = client.post("/ui/senders/1/toggle", cookies=session_cookie)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/senders?blocked=1"
    assert state["toggled"] is None            # never activated


def test_toggle_can_still_pause_a_non_allowlisted_sender(client, session_cookie, state, monkeypatch):
    """Pausing is always allowed — only activation is blocked."""
    monkeypatch.setattr(type(config), "SENDER_ALLOWED_ADDRESSES", ALLOW)
    state["sender"] = {**SENDER, "sender_email": "old@gmail.com", "active": True}
    r = client.post("/ui/senders/1/toggle", cookies=session_cookie)
    assert r.status_code == 303
    assert state["toggled"] == (1, False)      # paused


async def test_verified_senders_dropdown_filters_to_the_allowlist(monkeypatch):
    class FakeMailjet:
        def __init__(self, *a, **k):
            pass

        async def list_verified_senders(self):
            return ["business@renegadeinsurance.info", "nope@gmail.com"]

    monkeypatch.setattr(routes, "MailjetSender", FakeMailjet)
    monkeypatch.setattr(type(config), "SENDER_ALLOWED_ADDRESSES", ALLOW)
    addresses, error = await routes._verified_senders()
    assert addresses == ["business@renegadeinsurance.info"]
    assert error is None


# ---- per-address signature (migration 014) ---------------------------


def test_create_persists_a_signature(client, session_cookie, state):
    client.post("/ui/senders", cookies=session_cookie,
                data={"sender_email": "x@d.com", "signature": "Best,\nMadhav Gupta"})
    assert state["created"]["signature"] == "Best,\nMadhav Gupta"


def test_blank_signature_is_stored_as_none(client, session_cookie, state):
    client.post("/ui/senders", cookies=session_cookie,
                data={"sender_email": "x@d.com", "signature": "   "})
    assert state["created"]["signature"] is None
