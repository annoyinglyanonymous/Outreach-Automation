"""Structural invariants of the SQL in repo.py.

The suite is fakes-only, so nothing here executes a statement — these are
string assertions, which is a weak tool. They exist anyway because the
rules below are exactly the ones a reviewer is supposed to catch by eye,
and each has already been broken once in this file:

- every writer that acts on a claimed row must guard on the status it
  claimed, or a late write from a run that outlived its claim can drag a
  contact backwards through the pipeline;
- every predicate that can see a NULL column must survive three-valued
  logic, or rows disappear from reports that exist to explain them.

A statement added without its guard fails here rather than in production
three weeks later.
"""
from __future__ import annotations

import re

import pytest

from app import repo


# Every statement that mutates rows a stage has claimed, and the status
# it is only ever allowed to act on.
@pytest.mark.parametrize(("statement", "guard"), [
    ("WRITE_RESULTS_SQL",         "linkedin_status = 'enriching'"),
    ("WRITE_PROFILES_SQL",        "linkedin_status = 'scraping'"),
    ("WRITE_DRAFTS_SQL",          "linkedin_status = 'drafting'"),
    ("SET_RUN_ID_SQL",            "linkedin_status = 'scraping'"),
    ("RELEASE_SQL",               "linkedin_status = 'enriching'"),
    ("RELEASE_SCRAPE_CLAIMS_SQL", "linkedin_status = 'scraping'"),
    ("RELEASE_RUN_SQL",           "linkedin_status = 'scraping'"),
    ("RELEASE_DRAFT_SQL",         "linkedin_status = 'drafting'"),
    ("RELEASE_EMAIL_SQL",         "email_status = 'sending'"),
    ("MARK_EMAIL_SENT_SQL",       "email_status = 'sending'"),
    ("MARK_EMAIL_FAILED_SQL",     "email_status = 'sending'"),
])
def test_every_writer_guards_on_the_status_it_claimed(statement, guard):
    assert guard in getattr(repo, statement), (
        f"{statement} does not guard on {guard!r}. Without it, a write from "
        "a run whose claim was already reset by reset_stale_claims can act "
        "on a contact another pass has since moved on."
    )


def test_unsendable_report_uses_is_not_true_not_bare_negation():
    """Invariant: a campaign the claim skips must be explained somewhere.
    The report's inclusion is the NEGATION of the claim's sendable
    predicate; use ``(...) IS NOT TRUE`` rather than ``NOT (...)`` so a
    row is never dropped by three-valued logic (the original 2026-08-12
    fix, kept as the pool predicate replaced the consent one)."""
    sql = repo.UNSENDABLE_APPROVED_SQL
    assert "IS NOT TRUE" in sql
    assert "AND NOT (g.status = 'active'" not in sql


def test_a_redraft_cannot_regress_a_contact_that_is_already_sending():
    """Invariant 1 — at most one first-touch email per contact, ever.
    Only the email stage advances email_status; a re-draft may lift it
    from 'pending' and must otherwise leave it exactly as it found it."""
    sql = repo.WRITE_DRAFTS_SQL
    assert "WHEN c.email_status = 'pending'" in sql
    assert "ELSE c.email_status END" in sql


def test_the_edit_form_cannot_write_smartlead_campaign_id():
    """That column had a second, non-human writer (the Smartlead
    auto-setup). Including it in the full-row update let a form rendered
    BEFORE setup ran write its stale empty value back over the new id.
    Fixed 2026-08-12. The cold send-gate is now sender_email (Mailjet), so
    the column is inert — kept out of the edit update all the same, for
    rollback and to avoid resurrecting the regression.
    """
    assert "smartlead_campaign_id" not in repo.CAMPAIGN_UPDATE_FIELDS
    assert "smartlead_campaign_id" not in repo.UPDATE_CAMPAIGN_SQL
    # Creation still owns the column (writing NULL); only the edit path
    # is narrowed, so a new campaign still has it in its INSERT.
    assert "smartlead_campaign_id" in repo.CAMPAIGN_FIELDS
    assert "smartlead_campaign_id" in repo.CREATE_CAMPAIGN_SQL


def test_campaign_sql_placeholders_match_their_field_lists():
    """Both statements are f-string generated from a field tuple, so an
    off-by-one in the $n numbering is a runtime error against the live
    database rather than anything Python would catch."""
    create = {int(n) for n in re.findall(r"\$(\d+)", repo.CREATE_CAMPAIGN_SQL)}
    assert create == set(range(1, len(repo.CAMPAIGN_FIELDS) + 1))

    # $1 is the campaign id, then one placeholder per updatable field.
    update = {int(n) for n in re.findall(r"\$(\d+)", repo.UPDATE_CAMPAIGN_SQL)}
    assert update == set(range(1, len(repo.CAMPAIGN_UPDATE_FIELDS) + 2))


def test_no_stale_reset_ever_touches_a_sending_contact():
    """Invariant 4 — a crash between vendor-accept and our write leaves
    'sending' rows whose email DID go out. Resetting them would re-send,
    so they are counted and surfaced for a human instead."""
    resets = [name for name in dir(repo)
              if name.startswith("RESET_STALE") and name.endswith("_SQL")]
    assert resets, "no stale-reset statements found — did they get renamed?"
    for name in resets:
        assert "'sending'" not in getattr(repo, name), (
            f"{name} touches 'sending' rows; that re-sends already-sent mail."
        )


# ---- mailjet sender pool (migration 010) -----------------------------


def test_create_sender_sql_placeholders_match_the_field_list():
    create = {int(n) for n in re.findall(r"\$(\d+)", repo.CREATE_SENDER_SQL)}
    assert create == set(range(1, len(repo.MAILJET_SENDER_FIELDS) + 1))
    # $1 is the id, then one placeholder per updatable field.
    update = {int(n) for n in re.findall(r"\$(\d+)", repo.UPDATE_SENDER_SQL)}
    assert update == set(range(1, len(repo.MAILJET_SENDER_FIELDS) + 2))


def test_rotating_sender_pick_is_atomic_lru_and_resets_daily():
    """The pick must not double-hand a domain under concurrency, must be
    least-recently-used, and must reset the counter when the day rolls."""
    sql = repo.CLAIM_ROTATING_SENDER_SQL
    assert "FOR UPDATE SKIP LOCKED" in sql               # one row, one worker
    assert "ORDER BY last_used_at NULLS FIRST" in sql    # LRU
    assert "day < current_date" in sql                   # lazy daily reset
    assert "sent_today + 1" in sql                        # counts a real send


def test_sender_picks_return_the_signature():
    """Both send picks must return the per-address signature so the emailer
    can append it at send time (migration 014)."""
    assert "signature" in repo.MAILJET_SENDER_FIELDS
    assert "m.signature" in repo.CLAIM_ROTATING_SENDER_SQL
    assert "m.signature" in repo.CLAIM_PINNED_SENDER_SQL


def test_review_queries_carry_the_pinned_mailbox_signature():
    """The review card and preview show the send-time sign-off read-only. The
    signature lives on the SENDING mailbox and is only knowable in review when
    the campaign is pinned to one, so both review reads LEFT JOIN that mailbox
    and surface pinned_sender_id (rotating vs pinned) + its signature. LEFT
    JOIN, not INNER: a rotating campaign — or a pin to a since-deleted sender —
    must still return the contact row (the template then shows the 'varies per
    send' note), never drop the card."""
    for sql in (repo.REVIEW_QUEUE_SQL, repo.CONTACT_DETAIL_SQL):
        assert "g.pinned_sender_id" in sql
        assert "ps.signature AS pinned_signature" in sql
        assert "LEFT JOIN mailjet_senders ps ON ps.id = g.pinned_sender_id" in sql


def test_rotating_sender_release_floors_and_is_day_scoped():
    sql = repo.RELEASE_ROTATING_SENDER_SQL
    assert "GREATEST(sent_today - 1, 0)" in sql   # never negative
    assert "day = current_date" in sql            # don't touch a rolled-over counter


def test_email_claim_gates_on_pool_capacity_not_consent():
    """Mailjet is the only sender: sendability is pool capacity, and the
    claim carries no consent/sender columns any more."""
    sql = repo.CLAIM_EMAIL_SQL
    assert "mailjet_senders" in sql
    assert "m.sent_today < m.daily_cap" in sql
    assert "consent_status" not in sql
    assert "sender_email" not in sql


def test_claims_treat_daily_cap_zero_as_no_cap():
    """The drip (business-hours batch cadence) is the throttle now, so the
    per-sender daily cap is removed via a `daily_cap <= 0` = unlimited sentinel.
    All three capacity gates must honour it, or a cap-0 sender would read as
    `sent_today < 0` (never any room) and nothing would ever send."""
    assert "m.daily_cap <= 0" in repo.CLAIM_EMAIL_SQL
    assert "daily_cap <= 0" in repo.CLAIM_ROTATING_SENDER_SQL
    assert "daily_cap <= 0" in repo.CLAIM_PINNED_SENDER_SQL


def test_email_claim_can_scope_to_one_campaign():
    """The send-now override (emailer.send_campaign_now) reuses the claim with
    a single-campaign filter; a NULL $2 keeps the drip's cross-campaign claim
    exactly as it was."""
    sql = repo.CLAIM_EMAIL_SQL
    assert "c.campaign_id = $2" in sql
    assert "$2::bigint IS NULL" in sql


def test_email_claim_is_gated_on_test_approval():
    """No real email sends until the campaign's test is approved (migration
    017) — enforced in the claim, so the drip AND send_campaign_now respect it,
    and mirrored in the schedule forecast."""
    assert "g.test_status = 'approved'" in repo.CLAIM_EMAIL_SQL
    assert "g.test_status = 'approved'" in repo.APPROVED_UNSENT_SQL


def test_draft_claim_carries_the_stored_objective():
    """The campaign's stored objective is its drafting prompt (migration 018),
    so the claim must hand it to the drafter, and the edit form must own it."""
    assert "'objective',              g.objective" in repo.CLAIM_DRAFT_SQL
    assert "objective" in repo.CAMPAIGN_FIELDS
    assert "objective" in repo.CAMPAIGN_UPDATE_FIELDS   # edit-form owned


def test_draft_claim_is_gated_on_test_approval():
    """The gate sits on the FIRST money stage too: no LLM drafting until the
    test is approved — uploaded contacts wait at ready_to_draft. The lock stays
    on contacts only (FOR UPDATE OF c), matching the email claim's shape."""
    sql = repo.CLAIM_DRAFT_SQL
    assert "cg.test_status = 'approved'" in sql
    assert "FOR UPDATE OF c SKIP LOCKED" in sql


def test_unsendable_report_names_a_test_gated_campaign():
    """A campaign held for test approval must be surfaced (not silently
    missing), and its gate is part of the report's sendable predicate."""
    sql = repo.UNSENDABLE_APPROVED_SQL
    assert "held for test approval" in sql
    assert "g.test_status = 'approved'" in sql


def test_test_status_is_a_create_field_not_an_edit_field():
    """Created with the gate seeded; the edit form never writes it (only the
    test-send / approve actions do)."""
    assert "test_status" in repo.CAMPAIGN_FIELDS
    assert "test_status" not in repo.CAMPAIGN_UPDATE_FIELDS


def test_schedule_queries_mirror_the_claim_and_carry_the_mailbox():
    """The Schedule forecast reads the approved-unsent queue in the SAME order
    and with the same sendable predicate the claim uses (minus sender capacity,
    surfaced on the page), and the recent-sends log pulls the From mailbox from
    the email_sent event."""
    q = repo.APPROVED_UNSENT_SQL
    assert "email_status = 'drafted'" in q
    assert "review_status = 'approved'" in q
    assert "g.status = 'active'" in q
    assert "FROM suppression s" in q
    assert "ORDER BY c.created_at" in q          # the claim order
    assert "payload->>'sender'" in repo.RECENT_SENDS_SQL
    assert "email_status = 'sent_email'" in repo.RECENT_SENDS_SQL


def test_mark_email_sent_records_the_sender_mailbox():
    """The From is stored on the email_sent event so the log can group by
    mailbox (older sends, before this, read as unknown)."""
    assert "'sender', $4::text" in repo.MARK_EMAIL_SENT_SQL


def test_unsendable_report_names_an_empty_pool_not_a_capped_one():
    """An empty pool is a permanent misconfig worth naming; a merely-capped
    pool is transient pacing and must NOT show as unsendable — so the
    predicate checks active-existence, not remaining capacity."""
    sql = repo.UNSENDABLE_APPROVED_SQL
    assert "no active senders in the rotation pool" in sql
    assert "EXISTS (SELECT 1 FROM mailjet_senders m WHERE m.active)" in sql
    assert "IS NOT TRUE" in sql


# ---- single-mailbox pin (migration 011) ------------------------------


def test_pinned_sender_pick_is_atomic_and_scoped_to_one_id():
    """The pinned counterpart to the rotating pick: same concurrency-safe
    count-and-stamp and lazy daily reset, but locked to ONE id (WHERE id = $1)
    instead of picking LRU across the pool — so the per-domain cap still
    bounds a pinned campaign."""
    sql = repo.CLAIM_PINNED_SENDER_SQL
    assert "WHERE id = $1" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql   # one row, one worker
    assert "day < current_date" in sql       # lazy daily reset
    assert "sent_today + 1" in sql           # counts a real send
    # NOT an LRU pick across the pool — it is pinned to the one id.
    assert "ORDER BY last_used_at" not in sql


def test_email_claim_gate_is_per_campaign_pin_aware():
    """The capacity gate is per-campaign: a rotating campaign
    (pinned_sender_id IS NULL) needs any active sender with room, a pinned
    one needs its specific sender. The pin is carried out of the claim so the
    runner knows which sender to draw for each contact."""
    sql = repo.CLAIM_EMAIL_SQL
    assert "g.pinned_sender_id IS NULL" in sql
    assert "m.id = g.pinned_sender_id" in sql
    assert "claimed.pinned_sender_id" in sql   # returned to the runner


def test_unsendable_report_surfaces_a_pin_to_a_paused_mailbox():
    """A campaign pinned to a paused/deleted sender can never be claimed even
    while the pool has other active senders, so it must be surfaced (not
    silently dropped) — the report names it, and its inclusion predicate
    mirrors the claim's per-campaign gate."""
    sql = repo.UNSENDABLE_APPROVED_SQL
    assert "pinned sender inactive" in sql
    assert "m.id = g.pinned_sender_id" in sql


# ---- csv-only ingest (migration 012) ---------------------------------


def test_csv_insert_lands_contacts_ready_to_draft_pending():
    """CSV-only contacts must arrive at 'ready_to_draft' (invisible to the
    enrich/scrape/verify claims) with email_status 'pending' (so drafting can
    later lift it to 'drafted')."""
    sql = repo.INSERT_CSV_CONTACTS_SQL
    assert "INSERT INTO contacts" in sql
    assert "'ready_to_draft'" in sql
    assert "'pending'" in sql


def test_csv_insert_dedupes_and_respects_suppression():
    """Invariant 1 (one first-touch) + invariant 3 (suppression is terminal):
    in-batch dedupe, no second row for an address already in the campaign, and
    suppressed addresses are never inserted (so never drafted)."""
    sql = repo.INSERT_CSV_CONTACTS_SQL
    assert "DISTINCT ON (lower(email))" in sql
    assert "NOT EXISTS (SELECT 1 FROM contacts c" in sql
    assert "NOT EXISTS (SELECT 1 FROM suppression s" in sql


def test_draft_claim_carries_extra_data_and_mode():
    """The drafter needs the per-contact sheet columns and the campaign's mode
    to pick the CSV vs LinkedIn path."""
    sql = repo.CLAIM_DRAFT_SQL
    assert "c.extra_data" in sql
    assert "'enrichment_mode'" in sql


def test_enrichment_mode_is_a_campaign_field():
    assert "enrichment_mode" in repo.CAMPAIGN_FIELDS
    assert "enrichment_mode" in repo.CREATE_CAMPAIGN_SQL
    assert "enrichment_mode" in repo.UPDATE_CAMPAIGN_SQL


def test_send_mode_is_a_campaign_field_the_edit_form_owns():
    """Per-campaign send mode (migration 016): 'batch' drip vs 'immediate'
    drain. Create seeds it and the edit form writes it (unlike smartlead_id),
    so it must ride in BOTH field lists and BOTH statements — and the approve
    handler reads it via contact_send_context to route the send."""
    assert "send_mode" in repo.CAMPAIGN_FIELDS
    assert "send_mode" in repo.CAMPAIGN_UPDATE_FIELDS
    assert "send_mode" in repo.CREATE_CAMPAIGN_SQL
    assert "send_mode" in repo.UPDATE_CAMPAIGN_SQL
    assert "g.send_mode" in repo.CONTACT_SEND_CONTEXT_SQL


def test_pool_sync_auto_enrols_new_and_pauses_unverified():
    """Full auto-enrol: Mailjet's verified list drives pool membership.
    A verified address absent from the pool is INSERTed (active, default
    cap); an active row no longer verified is set inactive (paused, not
    deleted, so counters/history survive). The membership test is the
    unnested verified list, matched case-insensitively. Nothing here sets
    active = true on an existing row — that would silently undo an
    operator's manual pause of a still-verified sender."""
    sql = repo.SYNC_SENDERS_SQL
    assert "INSERT INTO mailjet_senders" in sql
    assert "unnest($1::text[], $2::text[])" in sql
    assert "NOT EXISTS" in sql          # only enrol addresses not already present
    assert "SET active = false" in sql  # pause the no-longer-verified
    assert "lower(" in sql              # case-insensitive membership
    assert "SET active = true" not in sql
