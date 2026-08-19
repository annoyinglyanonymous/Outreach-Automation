"""UI route tests — TestClient on an app that mounts only the UI router,
with repo and providers replaced by fakes (no database, no vendors)."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import drafting, repo, runs
from app.config import config
from app.providers import supabase_auth
from app.ui import router, routes
from app.ui.auth import COOKIE_NAME, sign_session

CONTACT = {
    "id": 1,
    "email": "jane@doe.example",
    "first_name": "Jane",
    "last_name": "Doe",
    "company": "Doe Insurance",
    "title": "Agency Owner",
    "linkedin_url": "https://www.linkedin.com/in/jane-doe",
    "linkedin_confidence": 0.62,
    "linkedin_status": "drafted",
    "review_status": "pending_review",
    "reviewed_at": None,
    "reviewed_by": None,
    "email_subject": "Subject",
    "email_body": "Body",
    "linkedin_note": "Note",
    "profile_data": {"headline": "Agency Owner"},
    "campaign_name": "Validation",
}


@pytest.fixture
def client(monkeypatch):
    # Class attributes, not instance ones: missing_ui_vars() is a
    # classmethod and reads the class.
    monkeypatch.setattr(type(config), "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(type(config), "SESSION_MAX_AGE_MINUTES", 480)
    monkeypatch.setattr(type(config), "SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setattr(type(config), "SUPABASE_ANON_KEY", "anon-key")

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def session_cookie(client):  # depends on client so the secret is set first
    return {COOKIE_NAME: sign_session("tester@example.com")}


@pytest.fixture
def calls(monkeypatch):
    seen: dict = {}

    async def update_draft(cid, subject, body, note, by):
        seen["update_draft"] = (cid, subject, body, note, by)
        return True

    async def set_review_status(cid, status, by):
        seen["set_review_status"] = (cid, status, by)
        return True

    async def requeue_for_redraft(cid, by):
        seen["redraft"] = (cid, by)
        return True

    async def confirm_enrichment(cid, by):
        seen["confirm"] = (cid, by)
        return True

    async def reject_enrichment(cid, by):
        seen["reject"] = (cid, by)
        return True

    async def contact_detail(cid):
        return dict(CONTACT, id=cid)

    async def contact_send_context(cid):
        # Approve routes on the campaign's send_mode; default 'batch' so the
        # existing approve tests keep asserting the drip nudge.
        return {"campaign_id": 1, "send_mode": "batch"}

    async def enrichment_review_queue(campaign_id=None, limit=100):
        return [dict(CONTACT, linkedin_status="enriched")]

    async def review_queue(status="pending_review", campaign_id=None, limit=50):
        # The real query filters by review_status, so the cards a tab shows
        # carry that status — the card template branches on it.
        return [dict(CONTACT, review_status=status)]

    async def verification_outcomes():
        return []

    async def recent_verifications(limit=20):
        return []

    async def list_campaigns():
        return []

    async def list_senders():
        # The campaign create/edit forms offer the pool as the "Sending
        # mailbox" dropdown; empty is fine for tests that don't assert on it.
        return []

    async def review_counts():
        return {"pending_review": 1}

    async def status_counts():
        return {"pending": 2}

    async def pending_runs():
        return []

    async def recent_events(limit=30, only_errors=False):
        return []

    for fn in (update_draft, set_review_status, requeue_for_redraft,
               confirm_enrichment, reject_enrichment, contact_detail,
               contact_send_context, enrichment_review_queue, review_queue,
               verification_outcomes, recent_verifications, list_campaigns,
               list_senders, review_counts, status_counts, pending_runs,
               recent_events):
        monkeypatch.setattr(repo, fn.__name__, fn)

    # Record nudges + immediate-sends instead of starting real background runs.
    seen["nudges"] = []
    seen["send_now"] = []
    monkeypatch.setattr(runs, "nudge", lambda stage: seen["nudges"].append(stage) or True)
    monkeypatch.setattr(runs, "send_now", lambda cid: seen["send_now"].append(cid) or True)

    return seen


# ---------------------------------------------------------------------
# auth behaviour
# ---------------------------------------------------------------------


def test_unauthenticated_page_redirects_to_login(client):
    response = client.get("/ui/")
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_unauthenticated_htmx_gets_hx_redirect(client):
    response = client.get("/ui/", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/ui/login"


def test_login_success_sets_cookie_and_redirects(client, monkeypatch):
    async def grant(email, password):
        return email == "rojan@example.com" and password == "pw"

    monkeypatch.setattr(supabase_auth, "password_grant", grant)

    response = client.post("/ui/login",
                           data={"email": "Rojan@Example.com", "password": "pw"})
    assert response.status_code == 303
    assert COOKIE_NAME in response.headers.get("set-cookie", "")


def test_login_wrong_password_rejected(client, monkeypatch):
    async def grant(email, password):
        return False

    monkeypatch.setattr(supabase_auth, "password_grant", grant)

    response = client.post("/ui/login", data={"email": "a@b.c", "password": "x"})
    assert response.status_code == 401
    assert b"Invalid email or password" in response.content


def test_cross_origin_post_rejected(client, session_cookie, calls):
    response = client.post(
        "/ui/contacts/1/enrichment/confirm",
        cookies=session_cookie,
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert "confirm" not in calls


# ---------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------


def test_dashboard_renders_without_run_buttons(client, session_cookie, calls):
    response = client.get("/ui/", cookies=session_cookie)
    assert response.status_code == 200
    assert b"Pipeline" in response.content
    # The manual per-stage Run buttons were removed; the pipeline self-runs.
    assert b"/ui/runs/" not in response.content
    assert b"Run enrich" not in response.content


def test_verify_page_lists_queue(client, session_cookie, calls):
    response = client.get("/ui/verify", cookies=session_cookie)
    assert response.status_code == 200
    assert b"Jane" in response.content
    assert b"linkedin.com/in/jane-doe" in response.content


def test_review_page_lists_cards(client, session_cookie, calls):
    response = client.get("/ui/review", cookies=session_cookie)
    assert response.status_code == 200
    assert b"Subject" in response.content


def test_review_page_says_when_more_are_waiting_than_shown(client, session_cookie,
                                                           calls, monkeypatch):
    """The queue renders at most 50 cards (repo default limit) while the tab
    count is the full total — the page must SAY so, or the mismatch reads as
    missing mail (live operator question, 2026-08-19)."""
    async def review_counts():
        return {"pending_review": 120}
    monkeypatch.setattr(repo, "review_counts", review_counts)

    body = client.get("/ui/review", cookies=session_cookie).text
    assert "Showing the oldest 1 of 120" in body   # calls fixture yields 1 card

    # And no hint when everything fits on one page.
    async def review_counts_small():
        return {"pending_review": 1}
    monkeypatch.setattr(repo, "review_counts", review_counts_small)
    body = client.get("/ui/review", cookies=session_cookie).text
    assert "Showing the oldest" not in body


def test_review_queue_sql_selects_linkedin_status():
    """The card template branches on linkedin_status; fakes bypass SQL,
    so pin the column list itself — omitting it rendered every card as
    'no longer in the drafted state' (live bug, 2026-08-11)."""
    assert "linkedin_status" in repo.REVIEW_QUEUE_SQL


def test_review_count_fragment_returns_pending(client, session_cookie, calls):
    """The sidebar badge poller reads this bare integer (review_counts fake
    returns pending_review=1)."""
    response = client.get("/ui/fragments/review-count", cookies=session_cookie)
    assert response.status_code == 200
    assert response.text.strip() == "1"


def test_review_count_fragment_requires_auth(client):
    # Same guard as every other fragment: an HX poll on an expired session
    # gets a 401 (the poller then just skips that tick).
    response = client.get("/ui/fragments/review-count", headers={"HX-Request": "true"})
    assert response.status_code == 401


def test_pending_tab_is_editable_with_verdict_buttons(client, session_cookie, calls):
    response = client.get("/ui/review?status=pending_review", cookies=session_cookie)
    body = response.text
    assert 'name="email_body"' in body            # editable textarea
    assert 'value="approve"' in body and 'value="reject"' in body


def test_resolved_tabs_are_readonly_without_verdict_buttons(client, session_cookie, calls):
    """Approved/rejected cards are an audit view — no editable fields and no
    verdict buttons (offering 'Save & approve' on an approved card is a
    confusing no-op). The draft text still shows."""
    for status in ("approved", "rejected"):
        body = client.get(f"/ui/review?status={status}", cookies=session_cookie).text
        assert 'name="email_body"' not in body     # not editable
        assert 'value="approve"' not in body
        assert 'value="reject"' not in body
        assert "Subject" in body                   # content still rendered


# ---------------------------------------------------------------------
# send-time signature, surfaced read-only in review + preview
# ---------------------------------------------------------------------


def test_rotating_campaign_shows_the_send_time_signature_note(
        client, session_cookie, calls):
    """The signature is appended at SEND time keyed on the From (emailer
    ._with_signature). A rotating campaign has no single From until then, so
    the card can't show one signature — it says the sign-off is added at send
    time and varies by mailbox. The default CONTACT fake carries no pin."""
    body = client.get("/ui/review", cookies=session_cookie).text
    assert "whichever verified mailbox the rotation picks" in body
    # It must NOT fabricate a concrete signature block for a rotating campaign.
    assert "sig-text" not in body


def test_pinned_campaign_shows_the_mailbox_signature_read_only(
        client, session_cookie, calls, monkeypatch):
    """A campaign pinned to one mailbox always signs as that mailbox, so its
    fixed signature is shown — but read-only (owned by the Senders page), never
    as an editable field the reviewer could duplicate into the body."""
    sig = "Warmly,\nDana Okafor\nRenegade Insurance"

    async def review_queue(status="pending_review", campaign_id=None, limit=50):
        return [dict(CONTACT, review_status=status,
                     pinned_sender_id=7, pinned_signature=sig)]
    monkeypatch.setattr(repo, "review_queue", review_queue)

    body = client.get("/ui/review", cookies=session_cookie).text
    assert "Dana Okafor" in body                          # the signature shows
    assert "appended automatically when this sends" in body
    assert "sig-text" in body
    # Read-only: the signature is never rendered inside an editable field.
    assert 'name="signature"' not in body


def test_pinned_mailbox_without_a_signature_warns_of_no_signoff(
        client, session_cookie, calls, monkeypatch):
    """Migration 014 flags a sender missing a signature; the review card must
    make the consequence visible — a pinned campaign whose mailbox has none
    sends with NO sign-off, rather than silently looking fine."""
    async def review_queue(status="pending_review", campaign_id=None, limit=50):
        return [dict(CONTACT, review_status=status,
                     pinned_sender_id=7, pinned_signature=None)]
    monkeypatch.setattr(repo, "review_queue", review_queue)

    body = client.get("/ui/review", cookies=session_cookie).text
    assert "no signature set" in body
    assert "sends with no sign-off" in body


def test_preview_shows_the_pinned_mailbox_signature(
        client, session_cookie, calls, monkeypatch):
    """The preview panel shows the same read-only sign-off: for a pinned
    campaign the route resolves that mailbox's signature (a repo lookup kept
    out of DB-free preview_draft) and the fragment renders it under the email."""
    sig = "Warmly,\nDana Okafor\nRenegade Insurance"
    seen: dict = {}

    async def get_preview_contact(campaign_id):
        return None

    async def get_sender(sender_id):
        seen["get_sender"] = sender_id
        return {"signature": sig}

    async def preview_draft(campaign, contact=None):
        return {"from": {"name": "You", "role": None}, "csv_mode": False,
                "source": "sample",
                "sample": {"name": "Sample", "title": "T",
                           "company": "C", "email": "s@e.co"},
                "template": {"subject": "S", "body": "Template body"},
                "personalized": None, "error": None, "note": None}

    monkeypatch.setattr(repo, "get_preview_contact", get_preview_contact)
    monkeypatch.setattr(repo, "get_sender", get_sender)
    monkeypatch.setattr(drafting, "preview_draft", preview_draft)

    response = client.post("/ui/campaigns/1/preview", cookies=session_cookie,
                           data={"pinned_sender_id": "7"})
    assert response.status_code == 200
    assert seen["get_sender"] == 7        # resolved the pinned mailbox
    assert "Dana Okafor" in response.text
    assert "sig-text" in response.text


# ---------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------


def test_confirm_enrichment_records_reviewer(client, session_cookie, calls):
    response = client.post("/ui/contacts/7/enrichment/confirm", cookies=session_cookie)
    assert response.status_code == 200
    assert calls["confirm"] == (7, "tester@example.com")


def test_reject_enrichment_records_reviewer(client, session_cookie, calls):
    response = client.post("/ui/contacts/7/enrichment/reject", cookies=session_cookie)
    assert response.status_code == 200
    assert calls["reject"] == (7, "tester@example.com")


def test_overlong_note_rejected_before_repo(client, session_cookie, calls):
    response = client.post("/ui/contacts/1/draft", cookies=session_cookie, data={
        "action": "save",
        "email_subject": "S",
        "email_body": "B",
        "linkedin_note": "x" * 400,
    })
    assert response.status_code == 422
    assert "update_draft" not in calls


def test_empty_subject_rejected(client, session_cookie, calls):
    response = client.post("/ui/contacts/1/draft", cookies=session_cookie, data={
        "action": "save", "email_subject": "", "email_body": "B",
    })
    assert response.status_code == 422
    assert "update_draft" not in calls


def test_approve_saves_edits_then_sets_status(client, session_cookie, calls):
    response = client.post("/ui/contacts/1/draft", cookies=session_cookie, data={
        "action": "approve",
        "email_subject": "Edited subject",
        "email_body": "Edited body",
        "linkedin_note": "Short note",
    })
    assert response.status_code == 200
    assert calls["update_draft"] == (1, "Edited subject", "Edited body",
                                     "Short note", "tester@example.com")
    assert calls["set_review_status"] == (1, "approved", "tester@example.com")
    # A 'batch' campaign (the fixture default) feeds the business-hours drip.
    assert calls["nudges"] == ["email"]
    assert calls["send_now"] == []          # not an immediate drain


def test_approve_on_immediate_campaign_drains_now_not_the_drip(
        client, session_cookie, calls, monkeypatch):
    """An 'immediate' campaign (migration 016) drains its whole approved queue
    at once on approval (runs.send_now -> emailer.send_campaign_now, which
    ignores the send window), instead of feeding the business-hours drip."""
    async def contact_send_context(cid):
        return {"campaign_id": 42, "send_mode": "immediate"}
    monkeypatch.setattr(repo, "contact_send_context", contact_send_context)

    response = client.post("/ui/contacts/1/draft", cookies=session_cookie, data={
        "action": "approve", "email_subject": "S", "email_body": "B",
    })

    assert response.status_code == 200
    assert calls["set_review_status"] == (1, "approved", "tester@example.com")
    assert calls["send_now"] == [42]        # drained this campaign now
    assert "email" not in calls["nudges"]   # NOT the drip


def test_reject_does_not_nudge_email(client, session_cookie, calls):
    client.post("/ui/contacts/1/draft", cookies=session_cookie, data={
        "action": "reject", "email_subject": "S", "email_body": "B",
    })
    assert calls["set_review_status"] == (1, "rejected", "tester@example.com")
    assert calls["nudges"] == []


def test_plain_save_does_not_touch_review_status(client, session_cookie, calls):
    client.post("/ui/contacts/1/draft", cookies=session_cookie, data={
        "action": "save", "email_subject": "S", "email_body": "B",
    })
    assert "update_draft" in calls
    assert "set_review_status" not in calls
    assert calls["nudges"] == []


def test_approve_collapses_card_and_refreshes_tabs(client, session_cookie, calls):
    """A verdict returns the collapsed acknowledgement plus an out-of-band
    tabs swap — not the editable form — so the card leaves the pending
    stack and the counts move without a page reload."""
    response = client.post("/ui/contacts/1/draft", cookies=session_cookie, data={
        "action": "approve", "email_subject": "S", "email_body": "B",
        "status": "pending_review",
    })
    assert response.status_code == 200
    body = response.text
    assert "Saved and approved." in body
    assert 'hx-swap-oob="true"' in body            # tabs update in place
    assert 'id="review-tabs"' in body
    assert "Awaiting review (1)" in body           # count comes from review_counts
    # The collapsed card must NOT re-render the editable form.
    assert 'name="email_body"' not in body


def test_plain_save_keeps_editable_card(client, session_cookie, calls):
    response = client.post("/ui/contacts/1/draft", cookies=session_cookie, data={
        "action": "save", "email_subject": "S", "email_body": "B",
    })
    assert response.status_code == 200
    body = response.text
    assert 'name="email_body"' in body             # still editable in place
    assert 'hx-swap-oob' not in body               # no tab swap on a plain save


def test_verdict_preserves_campaign_filter_in_tabs(client, session_cookie, calls):
    response = client.post("/ui/contacts/1/draft", cookies=session_cookie, data={
        "action": "reject", "email_subject": "S", "email_body": "B",
        "status": "pending_review", "campaign_id": "5",
    })
    assert "campaign_id=5" in response.text        # scope survives the action


def test_redraft_requeues(client, session_cookie, calls):
    response = client.post("/ui/contacts/1/redraft", cookies=session_cookie)
    assert response.status_code == 200
    assert calls["redraft"] == (1, "tester@example.com")


def test_run_trigger_route_is_gone(client, session_cookie):
    # The manual trigger endpoint was removed with the buttons; the pipeline
    # runs via the scheduler + nudges (and the JSON API's /*/run endpoints).
    assert client.post("/ui/runs/enrich", cookies=session_cookie).status_code == 404


def test_verify_queue_sql_is_profile_bearing_and_unverified():
    """The AI verify claim only takes matches WITH a scraped profile and no
    prior verdict — fakes bypass SQL, so pin the predicate itself."""
    sql = repo.VERIFY_QUEUE_SQL
    assert "profile_data IS NOT NULL" in sql
    assert "linkedin_url IS NOT NULL" in sql
    assert "enrichment_verified" in sql and "enrichment_rejected" in sql


def test_campaign_create_requires_name(client, session_cookie, calls):
    response = client.post("/ui/campaigns", cookies=session_cookie,
                           data={"name": "", "status": "active"})
    assert response.status_code == 422
    assert b"Name is required" in response.content


# ---------------------------------------------------------------------
# hostile / malformed input — all four fixed 2026-08-12
# ---------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/in/jane-doe",
    "https://linkedin.com/in/jane-doe",
    "https://np.linkedin.com/in/jane-doe",
])
def test_safe_linkedin_url_accepts_real_profiles(url):
    assert routes.safe_linkedin_url(url) == url


@pytest.mark.parametrize("url", [
    "http://www.linkedin.com/in/jane",          # not https
    "https://linkedin.com.evil.example/in/x",   # lookalike host
    "https://evil.example/.linkedin.com/pwn",   # in the path
    "https://evil.example/x?ref=.linkedin.com/",  # in the query
    "https://evil.example/#.linkedin.com/",     # in the fragment
    "javascript:alert(1)//.linkedin.com/",
    None,
    "",
])
def test_safe_linkedin_url_rejects_everything_else(url):
    """The value comes from a vendor, and the verify and review pages
    render it as a clickable link. The old check asked whether
    '.linkedin.com/' appeared anywhere in the string, so any attacker-
    chosen URL carrying that substring in its path, query or fragment
    rendered as a LinkedIn link."""
    assert routes.safe_linkedin_url(url) is None


def test_unicode_digit_in_a_flash_param_does_not_500(client, session_cookie,
                                                     calls, monkeypatch):
    """'²'.isdigit() is True but int('²') raises, so the guard on the
    ?ingested= flash let a hand-edited URL reach the browser as a 500."""
    async def get_campaign(campaign_id):
        fields = {f: "" for f in repo.CAMPAIGN_FIELDS}
        return dict(fields, id=campaign_id, name="Validation", status="active",
                    consent_status="cold", smartlead_campaign_id="sl-1",
                    sender_email=None)

    async def count_active_senders():
        return 0

    async def list_senders():
        return []

    monkeypatch.setattr(repo, "get_campaign", get_campaign)
    monkeypatch.setattr(repo, "count_active_senders", count_active_senders)
    monkeypatch.setattr(repo, "list_senders", list_senders)

    assert client.get("/ui/campaigns/1?ingested=3",
                      cookies=session_cookie).status_code == 200
    assert client.get("/ui/campaigns/1?ingested=²",
                      cookies=session_cookie).status_code == 200


def test_unicode_digit_in_the_campaign_filter_does_not_500(client, session_cookie,
                                                           calls):
    """Same guard, same bug, on the hidden campaign_id the review card
    posts back."""
    response = client.post("/ui/contacts/1/draft", cookies=session_cookie, data={
        "action": "save", "email_subject": "S", "email_body": "B",
        "campaign_id": "²",
    })
    assert response.status_code == 200


def test_a_verdict_that_did_not_apply_is_reported_not_collapsed(
        client, session_cookie, calls, monkeypatch):
    """The edit landed but the contact left 'drafted' in between, so no
    verdict was recorded. Collapsing the card would tell the reviewer the
    approval happened and move them on down the stack."""
    async def set_review_status(cid, status, by):
        return False

    monkeypatch.setattr(repo, "set_review_status", set_review_status)

    response = client.post("/ui/contacts/1/draft", cookies=session_cookie, data={
        "action": "approve", "email_subject": "S", "email_body": "B",
    })

    assert response.status_code == 200
    body = response.text
    assert "verdict did not apply" in body
    assert 'name="email_body"' in body      # editable card, not the collapsed one
    assert calls["nudges"] == []            # and the send gate stayed shut


def test_saving_a_campaign_cannot_revert_the_smartlead_id(client, session_cookie,
                                                          monkeypatch):
    """smartlead_campaign_id is inert now that cold sends via Mailjet, but
    it stays out of CAMPAIGN_UPDATE_FIELDS: a stray post of it (e.g. from an
    old cached form) must never round-trip into an UPDATE. Guards against
    resurrecting the stale-revert bug the exclusion was added to fix."""
    seen = {}

    async def update_campaign(campaign_id, fields):
        seen["fields"] = fields
        return True

    monkeypatch.setattr(repo, "update_campaign", update_campaign)

    response = client.post("/ui/campaigns/5", cookies=session_cookie, data={
        "name": "Validation",
        "tone": "warmer",
        "smartlead_campaign_id": "9999",   # a stray post must be ignored
    })

    assert response.status_code == 303
    assert "smartlead_campaign_id" not in seen["fields"]
    assert seen["fields"]["tone"] == "warmer"


def test_campaign_edit_offers_the_sending_mailbox_dropdown(client, session_cookie,
                                                           monkeypatch):
    """The edit form lets a campaign rotate across the pool (default) or pin
    to one mailbox: the pool renders as a select, the campaign's current pin
    is pre-selected, and a paused sender is flagged so it isn't picked blind."""
    async def get_campaign(campaign_id):
        fields = {f: "" for f in repo.CAMPAIGN_FIELDS}
        return dict(fields, id=campaign_id, name="Pinned", status="active",
                    pinned_sender_id=7)

    async def count_active_senders():
        return 2

    async def list_senders():
        return [{"id": 7, "sender_email": "one@d1.com", "active": True},
                {"id": 9, "sender_email": "two@d2.com", "active": False}]

    monkeypatch.setattr(repo, "get_campaign", get_campaign)
    monkeypatch.setattr(repo, "count_active_senders", count_active_senders)
    monkeypatch.setattr(repo, "list_senders", list_senders)

    body = client.get("/ui/campaigns/1", cookies=session_cookie).text
    assert 'name="pinned_sender_id"' in body
    assert "Rotate across all verified senders" in body
    assert re.search(r'value="7"\s+selected>', body)         # the current pin
    assert not re.search(r'value="9"\s+selected>', body)     # the other isn't
    assert "(paused)" in body                                # sender 9 flagged


def test_saving_a_campaign_pins_the_chosen_mailbox(client, session_cookie,
                                                   monkeypatch):
    """Choosing a mailbox persists as the bigint FK pinned_sender_id — an int,
    not the raw form string, so asyncpg binds it (a bare string would raise)."""
    seen = {}

    async def update_campaign(campaign_id, fields):
        seen["fields"] = fields
        return True

    monkeypatch.setattr(repo, "update_campaign", update_campaign)

    r = client.post("/ui/campaigns/5", cookies=session_cookie,
                    data={"name": "Pinned", "pinned_sender_id": "7"})

    assert r.status_code == 303
    assert seen["fields"]["pinned_sender_id"] == 7    # int, not "7"


def test_saving_a_campaign_with_no_mailbox_rotates_the_pool(client, session_cookie,
                                                            monkeypatch):
    """The empty 'Rotate across all verified senders' option clears the pin
    to NULL; a non-numeric value is treated the same, never a bad FK bind."""
    seen = {}

    async def update_campaign(campaign_id, fields):
        seen["fields"] = fields
        return True

    monkeypatch.setattr(repo, "update_campaign", update_campaign)

    for value in ("", "not-an-id"):
        r = client.post("/ui/campaigns/5", cookies=session_cookie,
                        data={"name": "Rotate", "pinned_sender_id": value})
        assert r.status_code == 303
        assert seen["fields"]["pinned_sender_id"] is None


def test_campaign_edit_offers_the_send_mode_dropdown(client, session_cookie,
                                                     monkeypatch):
    """The edit form exposes the send mode as a select, with the campaign's
    current choice pre-selected (here 'immediate')."""
    async def get_campaign(campaign_id):
        fields = {f: "" for f in repo.CAMPAIGN_FIELDS}
        return dict(fields, id=campaign_id, name="Now", status="active",
                    send_mode="immediate")

    async def count_active_senders():
        return 1

    async def list_senders():
        return []

    monkeypatch.setattr(repo, "get_campaign", get_campaign)
    monkeypatch.setattr(repo, "count_active_senders", count_active_senders)
    monkeypatch.setattr(repo, "list_senders", list_senders)

    body = client.get("/ui/campaigns/1", cookies=session_cookie).text
    assert 'name="send_mode"' in body
    assert "Send in batches" in body and "Send immediately" in body
    assert re.search(r'value="immediate"\s+selected>', body)      # current choice
    assert not re.search(r'value="batch"\s+selected>', body)


def test_saving_a_campaign_persists_the_send_mode(client, session_cookie,
                                                  monkeypatch):
    """The chosen send mode round-trips into the UPDATE, normalized to a
    CHECK-legal value; anything but 'immediate' falls back to 'batch' so a bad
    post never trips the DB CHECK."""
    seen = {}

    async def update_campaign(campaign_id, fields):
        seen["fields"] = fields
        return True

    monkeypatch.setattr(repo, "update_campaign", update_campaign)

    r = client.post("/ui/campaigns/5", cookies=session_cookie,
                    data={"name": "Now", "send_mode": "immediate"})
    assert r.status_code == 303
    assert seen["fields"]["send_mode"] == "immediate"

    r = client.post("/ui/campaigns/5", cookies=session_cookie,
                    data={"name": "Now", "send_mode": "garbage"})
    assert r.status_code == 303
    assert seen["fields"]["send_mode"] == "batch"       # normalized default


def test_upload_csv_mode_inserts_directly_and_nudges_draft(client, session_cookie,
                                                           calls, monkeypatch):
    """A 'csv' campaign upload inserts directly (repo.insert_csv_contacts),
    never touches n8n, and nudges draft — the contacts land at ready_to_draft."""
    inserted = {}

    async def get_campaign(cid):
        return {"id": cid, "enrichment_mode": "csv"}

    async def insert_csv_contacts(campaign_id, rows):
        inserted["args"] = (campaign_id, len(rows))
        return {"received": len(rows), "inserted": len(rows), "skipped": 0}

    async def n8n_ingest(cid, rows):
        raise AssertionError("n8n must not be called for a csv campaign")

    monkeypatch.setattr(repo, "get_campaign", get_campaign)
    monkeypatch.setattr(repo, "insert_csv_contacts", insert_csv_contacts)
    monkeypatch.setattr(routes.n8n, "ingest", n8n_ingest)

    r = client.post("/ui/campaigns/3/upload", cookies=session_cookie,
                    files={"file": ("c.csv", b"email,state\nx@y.com,TX\n", "text/csv")})

    assert r.status_code == 200
    assert inserted["args"] == (3, 1)
    assert calls["nudges"] == ["draft"]


def test_upload_linkedin_mode_still_uses_n8n_and_nudges_enrich(client, session_cookie,
                                                               calls, monkeypatch):
    ingested = {}

    async def get_campaign(cid):
        return {"id": cid, "enrichment_mode": "linkedin"}

    async def n8n_ingest(cid, rows):
        ingested["args"] = (cid, len(rows))
        return {"ok": True}

    monkeypatch.setattr(repo, "get_campaign", get_campaign)
    monkeypatch.setattr(routes.n8n, "ingest", n8n_ingest)

    r = client.post("/ui/campaigns/4/upload", cookies=session_cookie,
                    files={"file": ("c.csv", b"email\nx@y.com\n", "text/csv")})

    assert r.status_code == 200
    assert ingested["args"] == (4, 1)
    assert calls["nudges"] == ["enrich"]


def test_saving_a_campaign_sets_enrichment_mode(client, session_cookie, monkeypatch):
    """The edit form persists the mode, normalized to a CHECK-legal value
    (anything but 'csv' -> 'linkedin', so a hand-edited post can't 500)."""
    seen = {}

    async def update_campaign(campaign_id, fields):
        seen["fields"] = fields
        return True

    monkeypatch.setattr(repo, "update_campaign", update_campaign)

    r = client.post("/ui/campaigns/5", cookies=session_cookie,
                    data={"name": "X", "enrichment_mode": "csv"})
    assert r.status_code == 303
    assert seen["fields"]["enrichment_mode"] == "csv"

    r = client.post("/ui/campaigns/5", cookies=session_cookie,
                    data={"name": "X", "enrichment_mode": "bogus"})
    assert r.status_code == 303
    assert seen["fields"]["enrichment_mode"] == "linkedin"


def test_schedule_page_renders_batches_and_recent(client, session_cookie, monkeypatch):
    """The Schedule page projects the approved queue into batches (with the
    recipient + mailbox) and lists recent sends."""
    import datetime as _dt

    async def approved_unsent_queue(campaign_id=None, limit=500):
        return [{"id": 1, "email": "jane@x.com", "first_name": "Jane",
                 "last_name": "Doe", "company": "Doe Co", "campaign_id": 1,
                 "campaign_name": "Warmup", "pinned_sender_id": None}]

    async def active_senders_in_rotation_order():
        return [{"id": 1, "sender_email": "a@d1.com", "sender_name": "A"}]

    async def recent_sends(campaign_id=None, limit=100):
        return [{"id": 2, "email": "sam@x.com", "first_name": "Sam",
                 "last_name": "Ray", "company": "Ray Co", "campaign_name": "Warmup",
                 "email_sent_at": _dt.datetime(2026, 8, 19, 14, 0,
                                               tzinfo=_dt.timezone.utc),
                 "sender_email": "a@d1.com"}]

    async def list_campaigns():
        return [{"id": 1, "name": "Warmup", "contacts": 1}]

    monkeypatch.setattr(repo, "approved_unsent_queue", approved_unsent_queue)
    monkeypatch.setattr(repo, "active_senders_in_rotation_order",
                        active_senders_in_rotation_order)
    monkeypatch.setattr(repo, "recent_sends", recent_sends)
    monkeypatch.setattr(repo, "list_campaigns", list_campaigns)

    body = client.get("/ui/schedule", cookies=session_cookie).text
    assert "Send schedule" in body
    assert "jane@x.com" in body      # upcoming batch recipient
    assert "a@d1.com" in body        # projected From mailbox
    assert "sam@x.com" in body       # recent send


def test_test_send_drafts_sends_and_marks_sent(client, session_cookie, monkeypatch):
    """The test-send drafts a real sample, sends it to the typed address with
    the mailbox's signature, marks the gate 'sent', and flashes success."""
    seen = {}

    async def get_campaign(cid):
        return {"id": cid, "name": "W", "pinned_sender_id": None,
                "offer_description": "O", "cta": "C", "tone": "T",
                "sender_name": "", "sender_role": "", "audience_rationale": "A",
                "fallback_email_subject": "FS", "fallback_email_body": "FB",
                "enrichment_mode": "linkedin"}

    async def active_senders_in_rotation_order():
        return [{"id": 1, "sender_email": "a@d1.com", "sender_name": "A"}]

    async def get_sender(sid):
        return {"id": sid, "sender_email": "a@d1.com", "sender_name": "A",
                "signature": "Sig", "active": True}

    async def get_preview_contact(cid):
        return None

    async def preview_draft(campaign, contact=None):
        return {"personalized": {"subject": "S", "body": "B", "linkedin_note": None},
                "template": {"subject": "", "body": ""}, "error": None, "note": None}

    async def send_test_email(**kw):
        seen["sent"] = kw
        return "ref"

    async def mark_campaign_test_sent(cid):
        seen["marked"] = cid
        return True

    monkeypatch.setattr(repo, "get_campaign", get_campaign)
    monkeypatch.setattr(repo, "active_senders_in_rotation_order",
                        active_senders_in_rotation_order)
    monkeypatch.setattr(repo, "get_sender", get_sender)
    monkeypatch.setattr(repo, "get_preview_contact", get_preview_contact)
    monkeypatch.setattr(routes.drafting, "preview_draft", preview_draft)
    monkeypatch.setattr(routes.emailer, "send_test_email", send_test_email)
    monkeypatch.setattr(repo, "mark_campaign_test_sent", mark_campaign_test_sent)

    r = client.post("/ui/campaigns/3/test-send", cookies=session_cookie,
                    data={"test_email": "me@x.com"})
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/campaigns/3?test=sent"
    assert seen["sent"]["to_address"] == "me@x.com"
    assert seen["sent"]["signature"] == "Sig"
    assert seen["marked"] == 3


def test_test_send_rejects_a_bad_address(client, session_cookie, monkeypatch):
    async def get_campaign(cid):
        return {"id": cid, "pinned_sender_id": None}
    monkeypatch.setattr(repo, "get_campaign", get_campaign)

    r = client.post("/ui/campaigns/3/test-send", cookies=session_cookie,
                    data={"test_email": "not-an-email"})
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/campaigns/3?test=bad_address"


def test_test_approve_releases_nudges_draft_and_redirects(client, session_cookie,
                                                          calls, monkeypatch):
    """Approving the test releases the campaign AND starts drafting right away
    (drafting is gated on test approval, so the queue is waiting on this)."""
    seen = {}

    async def get_campaign(cid):
        return {"id": cid}

    async def approve_campaign_test(cid):
        seen["approved"] = cid
        return True

    monkeypatch.setattr(repo, "get_campaign", get_campaign)
    monkeypatch.setattr(repo, "approve_campaign_test", approve_campaign_test)

    r = client.post("/ui/campaigns/5/test-approve", cookies=session_cookie)
    assert r.status_code == 303
    assert r.headers["location"] == "/ui/campaigns/5?test=approved"
    assert seen["approved"] == 5
    assert calls["nudges"] == ["draft"]     # drafting starts on release


def test_send_now_sends_and_redirects_with_count(client, session_cookie, monkeypatch):
    """The per-campaign override calls emailer.send_campaign_now for that id and
    redirects back with the number sent (surfaced as a flash)."""
    seen = {}

    async def get_campaign(cid):
        return {"id": cid, "name": "Warmup"}

    async def send_campaign_now(campaign_id):
        seen["campaign_id"] = campaign_id
        return SimpleNamespace(sent=6)

    monkeypatch.setattr(repo, "get_campaign", get_campaign)
    monkeypatch.setattr(routes.emailer, "send_campaign_now", send_campaign_now)

    r = client.post("/ui/campaigns/9/send-now", cookies=session_cookie)

    assert r.status_code == 303
    assert seen["campaign_id"] == 9
    assert r.headers["location"] == "/ui/campaigns/9?sent_now=6"


def test_send_now_is_404_for_a_missing_campaign(client, session_cookie, monkeypatch):
    async def get_campaign(cid):
        return None
    monkeypatch.setattr(repo, "get_campaign", get_campaign)

    r = client.post("/ui/campaigns/99/send-now", cookies=session_cookie)
    assert r.status_code == 404


def test_sent_now_flash_renders_on_the_campaign_page(client, session_cookie, monkeypatch):
    """?sent_now=N surfaces a count flash; 0 surfaces the 'nothing sent' hint."""
    async def get_campaign(cid):
        fields = {f: "" for f in repo.CAMPAIGN_FIELDS}
        return dict(fields, id=cid, name="Warmup", status="active")

    async def count_active_senders():
        return 1

    async def list_senders():
        return []

    monkeypatch.setattr(repo, "get_campaign", get_campaign)
    monkeypatch.setattr(repo, "count_active_senders", count_active_senders)
    monkeypatch.setattr(repo, "list_senders", list_senders)

    hit = client.get("/ui/campaigns/1?sent_now=6", cookies=session_cookie).text
    assert "Sent 6 approved email(s) now" in hit
    miss = client.get("/ui/campaigns/1?sent_now=0", cookies=session_cookie).text
    assert "Nothing sent" in miss


def test_oversized_csv_is_refused_before_it_is_read(client, session_cookie,
                                                    monkeypatch):
    """CSV_MAX_BYTES used to be checked inside parse_contacts_csv, i.e.
    after the whole upload had already been read and buffered."""
    monkeypatch.setattr(type(config), "CSV_MAX_BYTES", 10)

    async def fake_get_campaign(cid):
        return {"id": cid, "enrichment_mode": "linkedin"}

    def explode(*args, **kwargs):
        raise AssertionError("oversized upload must not reach the parser")

    monkeypatch.setattr(repo, "get_campaign", fake_get_campaign)
    monkeypatch.setattr(routes, "parse_contacts_csv", explode)

    response = client.post(
        "/ui/campaigns/1/upload",
        cookies=session_cookie,
        files={"file": ("contacts.csv", b"email\n" + b"a@b.example\n" * 20,
                        "text/csv")},
    )

    assert response.status_code == 422
    assert b"limit is 10" in response.content
