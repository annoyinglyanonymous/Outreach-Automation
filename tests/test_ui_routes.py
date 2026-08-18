"""UI route tests — TestClient on an app that mounts only the UI router,
with repo and providers replaced by fakes (no database, no vendors)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import repo, runs
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
               enrichment_review_queue, review_queue, verification_outcomes,
               recent_verifications, list_campaigns, review_counts,
               status_counts, pending_runs, recent_events):
        monkeypatch.setattr(repo, fn.__name__, fn)

    # Record nudges instead of starting real background runs.
    seen["nudges"] = []
    monkeypatch.setattr(runs, "nudge", lambda stage: seen["nudges"].append(stage) or True)

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
    # Approval opens the send gate — the email run starts immediately.
    assert calls["nudges"] == ["email"]


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

    monkeypatch.setattr(repo, "get_campaign", get_campaign)

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


def test_oversized_csv_is_refused_before_it_is_read(client, session_cookie,
                                                    monkeypatch):
    """CSV_MAX_BYTES used to be checked inside parse_contacts_csv, i.e.
    after the whole upload had already been read and buffered."""
    monkeypatch.setattr(type(config), "CSV_MAX_BYTES", 10)

    def explode(data):
        raise AssertionError("oversized upload must not reach the parser")

    monkeypatch.setattr(routes, "parse_contacts_csv", explode)

    response = client.post(
        "/ui/campaigns/1/upload",
        cookies=session_cookie,
        files={"file": ("contacts.csv", b"email\n" + b"a@b.example\n" * 20,
                        "text/csv")},
    )

    assert response.status_code == 422
    assert b"limit is 10" in response.content
