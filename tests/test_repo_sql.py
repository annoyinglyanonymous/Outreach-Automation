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


def test_unsendable_report_survives_a_null_consent_status():
    """Invariant: a campaign the claim skips must be explained somewhere.

    ``NOT (... consent_status = ANY($1) ...)`` is NULL — not TRUE — when
    consent_status is NULL, so the campaign vanished from this report
    while also never being claimed: approved contacts stuck with nothing
    on the dashboard to say why. Fixed 2026-08-12.
    """
    sql = repo.UNSENDABLE_APPROVED_SQL
    assert "IS NOT TRUE" in sql
    assert "AND NOT (g.status = 'active'" not in sql
    # The CASE branch that names a NULL consent must stay reachable.
    assert "coalesce(g.consent_status, 'NULL')" in sql


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
