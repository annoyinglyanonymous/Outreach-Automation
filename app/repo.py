"""All SQL lives here. Nothing else in the service touches the database."""
from __future__ import annotations

import json
from dataclasses import dataclass

from .config import config
from .db import pool


@dataclass(slots=True)
class Contact:
    id: int
    email: str
    first_name: str
    last_name: str | None
    company: str | None
    title: str | None


@dataclass(slots=True)
class ScrapeTarget:
    id: int
    linkedin_url: str


@dataclass(slots=True)
class EmailTarget:
    id: int
    email: str
    first_name: str
    last_name: str | None
    company: str | None
    email_subject: str
    email_body: str
    consent_status: str | None
    smartlead_campaign_id: str | None
    sender_email: str | None
    sender_name: str | None


@dataclass(slots=True)
class DraftTarget:
    id: int
    email: str
    first_name: str
    last_name: str | None
    company: str | None
    title: str | None
    linkedin_url: str | None
    profile_data: dict | None
    # Campaign fields the drafter needs (offer, cta, tone, sender,
    # audience_rationale, fallback_email_*), joined in by the claim query
    # so the runner never issues a second lookup per contact.
    campaign: dict


@dataclass
class VerifyTarget:
    """What the AI verifier compares: the intended contact (from the
    source list) against the profile a vendor matched + scraped."""
    id: int
    email: str
    first_name: str
    last_name: str | None
    company: str | None
    title: str | None
    linkedin_url: str | None
    profile_data: dict | None


# =====================================================================
# enrichment stage
# =====================================================================

RESET_STALE_SQL = """
UPDATE contacts
   SET linkedin_status = 'pending'
 WHERE linkedin_status = 'enriching'
   AND updated_at < now() - ($1::int * INTERVAL '1 minute')
RETURNING id;
"""


async def reset_stale_claims() -> int:
    rows = await pool().fetch(RESET_STALE_SQL, config.STALE_CLAIM_MINUTES)
    return len(rows)


CLAIM_SQL = """
WITH claimed AS (
    SELECT id
      FROM contacts
     WHERE linkedin_status = 'pending'
     ORDER BY created_at
     LIMIT $1
     FOR UPDATE SKIP LOCKED
)
UPDATE contacts c
   SET linkedin_status = 'enriching'
  FROM claimed
 WHERE c.id = claimed.id
RETURNING c.id, c.email, c.first_name, c.last_name, c.company, c.title;
"""


async def claim_batch(limit: int | None = None) -> list[Contact]:
    rows = await pool().fetch(CLAIM_SQL, limit or config.BATCH_SIZE)
    return [
        Contact(
            id=r["id"],
            email=r["email"],
            first_name=r["first_name"],
            last_name=r["last_name"],
            company=r["company"],
            title=r["title"],
        )
        for r in rows
    ]


CACHE_SQL = """
SELECT DISTINCT ON (email)
       email, linkedin_url, linkedin_confidence
  FROM contacts
 WHERE email = ANY($1::text[])
   AND linkedin_url IS NOT NULL
   AND linkedin_status <> 'pending'
   AND updated_at > now() - ($2::int * INTERVAL '1 day')
   AND linkedin_confidence >= $3
 ORDER BY email, updated_at DESC;
"""


async def cache_lookup(emails: list[str]) -> dict[str, tuple[str, float]]:
    if not emails:
        return {}
    rows = await pool().fetch(
        CACHE_SQL, emails, config.CACHE_MAX_AGE_DAYS, config.CACHE_MIN_CONFIDENCE
    )
    return {
        r["email"]: (r["linkedin_url"], float(r["linkedin_confidence"]))
        for r in rows
    }


# Guarded on 'enriching' like every other claim-writer here: a slow
# provider call can outlive STALE_CLAIM_MINUTES, and once reset_stale_claims
# has returned the row to 'pending' another pass may already have carried it
# to ready_to_draft/drafted. Without the guard that late write would drag it
# back to 'enriched' and re-pay for a scrape it already had.
WRITE_RESULTS_SQL = """
WITH payload AS (
    SELECT * FROM json_to_recordset($1::json) AS x(
        id bigint, linkedin_url text, linkedin_confidence numeric, tier text
    )
)
UPDATE contacts c
   SET linkedin_url        = p.linkedin_url,
       linkedin_confidence = p.linkedin_confidence,
       linkedin_status     = CASE WHEN p.linkedin_url IS NULL
                                  THEN 'ready_to_draft'
                                  ELSE 'enriched' END,
       last_action_at      = now()
  FROM payload p
 WHERE c.id = p.id
   AND c.linkedin_status = 'enriching'
RETURNING c.id;
"""

LOG_EVENTS_SQL = """
INSERT INTO events (contact_id, channel, event_type, payload)
SELECT x.id,
       'linkedin',
       CASE WHEN x.linkedin_url IS NULL THEN 'enrichment_failed' ELSE 'enriched' END,
       json_build_object('tier', x.tier, 'confidence', x.linkedin_confidence)::jsonb
  FROM json_to_recordset($1::json) AS x(
        id bigint, linkedin_url text, linkedin_confidence numeric, tier text
  );
"""


async def write_results(results: list[dict]) -> int:
    if not results:
        return 0
    blob = json.dumps(results)
    async with pool().acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(WRITE_RESULTS_SQL, blob)
            await conn.execute(LOG_EVENTS_SQL, blob)
    return len(rows)


RELEASE_SQL = """
UPDATE contacts
   SET linkedin_status = 'pending'
 WHERE id = ANY($1::bigint[])
   AND linkedin_status = 'enriching'
RETURNING id;
"""


async def release_claims(ids: list[int]) -> int:
    if not ids:
        return 0
    rows = await pool().fetch(RELEASE_SQL, ids)
    return len(rows)


async def pending_count() -> int:
    """How much work is left. Used for logging and the health endpoint."""
    row = await pool().fetchrow(
        "SELECT count(*)::int AS n FROM contacts WHERE linkedin_status = 'pending';"
    )
    return row["n"]


# =====================================================================
# scraping stage (Apify)
#
# Unlike enrichment, the vendor side is asynchronous: a claimed batch
# becomes one actor run, the run id is stamped on the rows, and a later
# pass reconciles whatever runs have finished. Status stays 'scraping'
# for the whole round trip.
# =====================================================================

CLAIM_SCRAPE_SQL = """
WITH claimed AS (
    SELECT id
      FROM contacts
     WHERE linkedin_status = 'enriched'
       AND linkedin_url IS NOT NULL
     ORDER BY created_at
     LIMIT $1
     FOR UPDATE SKIP LOCKED
)
UPDATE contacts c
   SET linkedin_status = 'scraping'
  FROM claimed
 WHERE c.id = claimed.id
RETURNING c.id, c.linkedin_url;
"""


async def claim_scrape_batch(limit: int | None = None) -> list[ScrapeTarget]:
    rows = await pool().fetch(CLAIM_SCRAPE_SQL, limit or config.SCRAPE_BATCH_SIZE)
    return [ScrapeTarget(id=r["id"], linkedin_url=r["linkedin_url"]) for r in rows]


SET_RUN_ID_SQL = """
UPDATE contacts
   SET apify_run_id = $1
 WHERE id = ANY($2::bigint[])
   AND linkedin_status = 'scraping'
RETURNING id;
"""


async def set_run_id(run_id: str, ids: list[int]) -> int:
    rows = await pool().fetch(SET_RUN_ID_SQL, run_id, ids)
    return len(rows)


# Oldest first, so a run that keeps erroring cannot starve newer runs
# of collection attempts.
PENDING_RUNS_SQL = """
SELECT apify_run_id AS run_id
  FROM contacts
 WHERE linkedin_status = 'scraping'
   AND apify_run_id IS NOT NULL
 GROUP BY apify_run_id
 ORDER BY min(updated_at);
"""


async def pending_runs() -> list[str]:
    rows = await pool().fetch(PENDING_RUNS_SQL)
    return [r["run_id"] for r in rows]


RUN_CONTACTS_SQL = """
SELECT id, linkedin_url
  FROM contacts
 WHERE linkedin_status = 'scraping'
   AND apify_run_id = $1;
"""


async def run_contacts(run_id: str) -> list[ScrapeTarget]:
    rows = await pool().fetch(RUN_CONTACTS_SQL, run_id)
    return [ScrapeTarget(id=r["id"], linkedin_url=r["linkedin_url"]) for r in rows]


# profile IS NULL means the actor could not scrape this one profile —
# an outcome, not a position, so the contact still moves forward and the
# failure is recorded in events (drafting falls back to the template).
WRITE_PROFILES_SQL = """
WITH payload AS (
    SELECT * FROM json_to_recordset($1::json) AS x(
        id bigint, profile jsonb, run_id text
    )
)
UPDATE contacts c
   SET profile_data       = p.profile,
       profile_scraped_at = CASE WHEN p.profile IS NULL THEN NULL ELSE now() END,
       linkedin_status    = 'ready_to_draft',
       last_action_at     = now()
  FROM payload p
 WHERE c.id = p.id
   AND c.linkedin_status = 'scraping'
RETURNING c.id;
"""

LOG_SCRAPE_EVENTS_SQL = """
INSERT INTO events (contact_id, channel, event_type, payload)
SELECT x.id,
       'linkedin',
       CASE WHEN x.profile IS NULL THEN 'scrape_failed' ELSE 'profile_scraped' END,
       json_build_object('apify_run_id', x.run_id)::jsonb
  FROM json_to_recordset($1::json) AS x(
        id bigint, profile jsonb, run_id text
  );
"""


async def write_profiles(results: list[dict]) -> int:
    if not results:
        return 0
    blob = json.dumps(results)
    async with pool().acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(WRITE_PROFILES_SQL, blob)
            await conn.execute(LOG_SCRAPE_EVENTS_SQL, blob)
    return len(rows)


# A failed/aborted/expired run is the vendor's failure, not the contacts':
# back to 'enriched' so a later pass re-scrapes them.
RELEASE_RUN_SQL = """
UPDATE contacts
   SET linkedin_status = 'enriched',
       apify_run_id    = NULL
 WHERE apify_run_id = $1
   AND linkedin_status = 'scraping'
RETURNING id;
"""


async def release_run(run_id: str) -> int:
    rows = await pool().fetch(RELEASE_RUN_SQL, run_id)
    return len(rows)


RELEASE_SCRAPE_CLAIMS_SQL = """
UPDATE contacts
   SET linkedin_status = 'enriched',
       apify_run_id    = NULL
 WHERE id = ANY($1::bigint[])
   AND linkedin_status = 'scraping'
RETURNING id;
"""


async def release_scrape_claims(ids: list[int]) -> int:
    if not ids:
        return 0
    rows = await pool().fetch(RELEASE_SCRAPE_CLAIMS_SQL, ids)
    return len(rows)


# Only rows with no run id are stale: a crash between claiming and the
# actor run starting leaves them stranded. Rows *with* a run id are
# legitimately waiting on Apify and are the collector's job.
RESET_STALE_SCRAPE_SQL = """
UPDATE contacts
   SET linkedin_status = 'enriched'
 WHERE linkedin_status = 'scraping'
   AND apify_run_id IS NULL
   AND updated_at < now() - ($1::int * INTERVAL '1 minute')
RETURNING id;
"""


async def reset_stale_scrape_claims() -> int:
    rows = await pool().fetch(RESET_STALE_SCRAPE_SQL, config.STALE_CLAIM_MINUTES)
    return len(rows)


# =====================================================================
# drafting stage
#
# Requires migration 003 (adds the 'drafting' status). The claim joins
# campaign fields in and aliases them to the names the drafter uses, so
# the runner never issues a second lookup and never sees column names.
# =====================================================================

CLAIM_DRAFT_SQL = """
WITH claimed AS (
    SELECT id, campaign_id
      FROM contacts
     WHERE linkedin_status = 'ready_to_draft'
     ORDER BY created_at
     LIMIT $1
     FOR UPDATE SKIP LOCKED
)
UPDATE contacts c
   SET linkedin_status = 'drafting'
  FROM claimed
  LEFT JOIN campaigns g ON g.id = claimed.campaign_id
 WHERE c.id = claimed.id
RETURNING c.id, c.email, c.first_name, c.last_name, c.company, c.title,
          c.linkedin_url, c.profile_data,
          jsonb_build_object(
              'offer',                  g.offer_description,
              'cta',                    g.cta,
              'tone',                   g.tone,
              'sender',                 g.sender_name,
              'sender_role',            g.sender_role,
              'audience_rationale',     g.audience_rationale,
              'fallback_email_subject', g.fallback_email_subject,
              'fallback_email_body',    g.fallback_email_body
          ) AS campaign;
"""


def _jsonb(value) -> dict | None:
    # asyncpg returns jsonb as text unless a codec is registered; with
    # statement_cache_size=0 a per-connection codec is more machinery
    # than two json.loads calls.
    if isinstance(value, str):
        return json.loads(value)
    return value


async def claim_draft_batch(limit: int | None = None) -> list[DraftTarget]:
    rows = await pool().fetch(CLAIM_DRAFT_SQL, limit or config.DRAFT_BATCH_SIZE)
    return [
        DraftTarget(
            id=r["id"],
            email=r["email"],
            first_name=r["first_name"],
            last_name=r["last_name"],
            company=r["company"],
            title=r["title"],
            linkedin_url=r["linkedin_url"],
            profile_data=_jsonb(r["profile_data"]),
            campaign=_jsonb(r["campaign"]) or {},
        )
        for r in rows
    ]


# email_status only advances from 'pending': a re-draft must never
# regress a contact whose email is already sending or sent. The review
# fields reset on every write so an approval can never outlive the copy
# it approved (a rejected contact that gets re-drafted needs review again).
WRITE_DRAFTS_SQL = """
WITH payload AS (
    SELECT * FROM json_to_recordset($1::json) AS x(
        id bigint, email_subject text, email_body text,
        linkedin_note text, path text
    )
)
UPDATE contacts c
   SET email_subject   = p.email_subject,
       email_body      = p.email_body,
       linkedin_note   = p.linkedin_note,
       linkedin_status = 'drafted',
       review_status   = 'pending_review',
       reviewed_at     = NULL,
       reviewed_by     = NULL,
       email_status    = CASE WHEN c.email_status = 'pending'
                              THEN 'drafted' ELSE c.email_status END,
       last_action_at  = now()
  FROM payload p
 WHERE c.id = p.id
   AND c.linkedin_status = 'drafting'
RETURNING c.id;
"""

LOG_DRAFT_EVENTS_SQL = """
INSERT INTO events (contact_id, channel, event_type, payload)
SELECT x.id,
       'email',
       'drafted',
       json_build_object('path', x.path)::jsonb
  FROM json_to_recordset($1::json) AS x(
        id bigint, email_subject text, email_body text,
        linkedin_note text, path text
  );
"""


async def write_drafts(results: list[dict]) -> int:
    if not results:
        return 0
    blob = json.dumps(results)
    async with pool().acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(WRITE_DRAFTS_SQL, blob)
            await conn.execute(LOG_DRAFT_EVENTS_SQL, blob)
    return len(rows)


RELEASE_DRAFT_SQL = """
UPDATE contacts
   SET linkedin_status = 'ready_to_draft'
 WHERE id = ANY($1::bigint[])
   AND linkedin_status = 'drafting'
RETURNING id;
"""


async def release_draft_claims(ids: list[int]) -> int:
    if not ids:
        return 0
    rows = await pool().fetch(RELEASE_DRAFT_SQL, ids)
    return len(rows)


RESET_STALE_DRAFT_SQL = """
UPDATE contacts
   SET linkedin_status = 'ready_to_draft'
 WHERE linkedin_status = 'drafting'
   AND updated_at < now() - ($1::int * INTERVAL '1 minute')
RETURNING id;
"""


async def reset_stale_draft_claims() -> int:
    rows = await pool().fetch(RESET_STALE_DRAFT_SQL, config.STALE_CLAIM_MINUTES)
    return len(rows)


# =====================================================================
# email sending stage
#
# The gate is  email_status = 'drafted' AND review_status = 'approved',
# and only this stage ever moves email_status forward — which is what
# makes "at most one first-touch email per contact" hold: no sequence
# of review/redraft actions can return a sent contact to 'drafted'.
#
# There is deliberately NO stale-claim reset here. A crash between the
# vendor accepting a send and our write leaves 'sending' rows whose
# email DID go out; resetting them to 'drafted' would re-send. They are
# counted, surfaced, and resolved by a human instead.
# =====================================================================

# The claim picks only rows that can actually send right now: active
# campaign, a consent path whose API key is configured ($1), and a verified
# sender address on the campaign. Both send paths are now transactional
# ESPs (cold -> Mailjet, opted_in -> Resend) that send from a verified
# sender_email, so the readiness column is the same for both. Misconfigured
# campaigns are never claimed (see unsendable_approved_counts) so they
# cannot starve others.
CLAIM_EMAIL_SQL = """
WITH claimed AS (
    SELECT c.id
      FROM contacts c
      JOIN campaigns g ON g.id = c.campaign_id
     WHERE c.email_status = 'drafted'
       AND c.review_status = 'approved'
       AND g.status = 'active'
       AND g.consent_status = ANY($1::text[])
       AND g.sender_email IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM suppression s
                        WHERE lower(s.email) = lower(c.email))
     ORDER BY c.created_at
     LIMIT $2
     FOR UPDATE OF c SKIP LOCKED
)
UPDATE contacts c
   SET email_status   = 'sending',
       last_action_at = now()
  FROM claimed, campaigns g
 WHERE c.id = claimed.id
   AND g.id = c.campaign_id
RETURNING c.id, c.email, c.first_name, c.last_name, c.company,
          c.email_subject, c.email_body,
          g.consent_status, g.smartlead_campaign_id, g.sender_email,
          g.sender_name;
"""


async def claim_email_batch(consents: list[str],
                            limit: int | None = None) -> list[EmailTarget]:
    if not consents:
        return []
    rows = await pool().fetch(CLAIM_EMAIL_SQL, consents,
                              limit or config.SEND_BATCH_SIZE)
    return [
        EmailTarget(
            id=r["id"],
            email=r["email"],
            first_name=r["first_name"],
            last_name=r["last_name"],
            company=r["company"],
            email_subject=r["email_subject"],
            email_body=r["email_body"],
            consent_status=r["consent_status"],
            smartlead_campaign_id=r["smartlead_campaign_id"],
            sender_email=r["sender_email"],
            sender_name=r["sender_name"],
        )
        for r in rows
    ]


# Suppression trumps approval: the sweep keys on email_status alone so a
# rejected-but-suppressed contact is settled too, not re-swept forever.
SWEEP_SUPPRESSED_SQL = """
WITH swept AS (
    UPDATE contacts c
       SET email_status   = 'suppressed',
           last_action_at = now()
     WHERE c.email_status = 'drafted'
       AND EXISTS (SELECT 1 FROM suppression s
                    WHERE lower(s.email) = lower(c.email))
    RETURNING c.id
)
INSERT INTO events (contact_id, channel, event_type, payload)
SELECT id, 'email', 'email_suppressed', '{}'::jsonb FROM swept
RETURNING contact_id;
"""


async def sweep_suppressed() -> int:
    rows = await pool().fetch(SWEEP_SUPPRESSED_SQL)
    return len(rows)


# "Sent" means handed to delivery infrastructure. Smartlead schedules
# the actual delivery itself; true delivery/reply tracking arrives with
# the webhook phase.
MARK_EMAIL_SENT_SQL = """
WITH updated AS (
    UPDATE contacts
       SET email_status   = 'sent_email',
           email_sent_at  = now(),
           last_action_at = now()
     WHERE id = $1 AND email_status = 'sending'
    RETURNING id
)
INSERT INTO events (contact_id, channel, event_type, payload)
SELECT id, 'email', 'email_sent',
       json_build_object('provider', $2::text, 'ref', $3::text)::jsonb
  FROM updated
RETURNING contact_id;
"""


async def mark_email_sent(contact_id: int, provider: str, ref: str) -> bool:
    row = await pool().fetchrow(MARK_EMAIL_SENT_SQL, contact_id, provider, ref)
    return row is not None


# 'failed' is terminal and not re-claimable: re-sending after a hard
# rejection is a human decision, not something a retry loop should do.
MARK_EMAIL_FAILED_SQL = """
WITH updated AS (
    UPDATE contacts
       SET email_status   = 'failed',
           last_action_at = now()
     WHERE id = $1 AND email_status = 'sending'
    RETURNING id
)
INSERT INTO events (contact_id, channel, event_type, payload)
SELECT id, 'email', 'email_send_failed',
       json_build_object('provider', $2::text, 'reason', $3::text)::jsonb
  FROM updated
RETURNING contact_id;
"""


async def mark_email_failed(contact_id: int, provider: str, reason: str) -> bool:
    row = await pool().fetchrow(MARK_EMAIL_FAILED_SQL, contact_id, provider,
                                reason[:500])
    return row is not None


RELEASE_EMAIL_SQL = """
UPDATE contacts
   SET email_status = 'drafted'
 WHERE id = ANY($1::bigint[])
   AND email_status = 'sending'
RETURNING id;
"""


async def release_email_claims(ids: list[int]) -> int:
    if not ids:
        return 0
    rows = await pool().fetch(RELEASE_EMAIL_SQL, ids)
    return len(rows)


STUCK_SENDING_SQL = """
SELECT count(*)::int AS n
  FROM contacts
 WHERE email_status = 'sending'
   AND updated_at < now() - ($1::int * INTERVAL '1 minute');
"""


async def count_stuck_sending() -> int:
    row = await pool().fetchrow(STUCK_SENDING_SQL, config.STALE_CLAIM_MINUTES)
    return row["n"]


# Approved contacts the claim will never pick up, grouped by why — the
# loud counterpart to the claim silently skipping misconfiguration.
UNSENDABLE_APPROVED_SQL = """
SELECT g.name AS campaign_name,
       CASE
         WHEN g.status <> 'active'
             THEN 'campaign not active'
         WHEN NOT (g.consent_status = ANY($1::text[]))
             THEN 'no API key configured for consent ' || coalesce(g.consent_status, 'NULL')
         WHEN g.sender_email IS NULL
             THEN 'missing sender_email'
         ELSE 'unsupported consent ' || coalesce(g.consent_status, 'NULL')
       END AS reason,
       count(*)::int AS contacts
  FROM contacts c
  JOIN campaigns g ON g.id = c.campaign_id
 WHERE c.email_status = 'drafted'
   AND c.review_status = 'approved'
   -- IS NOT TRUE, not NOT (...): with consent_status NULL the whole
   -- predicate is NULL, and NOT NULL is NULL — so a campaign with no
   -- consent set would be filtered out of this report while also never
   -- being claimed, i.e. silently stuck with nothing to explain it. The
   -- CASE above already has a branch naming that case; this is what lets
   -- it be reached.
   AND (g.status = 'active'
    AND g.consent_status = ANY($1::text[])
    AND g.sender_email IS NOT NULL) IS NOT TRUE
 GROUP BY 1, 2
 ORDER BY 1, 2;
"""


async def unsendable_approved_counts(consents: list[str]) -> list[dict]:
    rows = await pool().fetch(UNSENDABLE_APPROVED_SQL, consents)
    return [dict(r) for r in rows]


async def email_status_counts() -> dict[str, int]:
    rows = await pool().fetch(
        "SELECT email_status, count(*)::int AS n FROM contacts GROUP BY email_status;"
    )
    return {r["email_status"]: r["n"] for r in rows}


# =====================================================================
# ui: enrichment verification
#
# Reviewed-or-not is derived from events rather than a column: the
# verdict is an audit fact, and the queue query is cheap at this scale.
# =====================================================================

ENRICHMENT_REVIEW_QUEUE_SQL = """
SELECT c.id, c.email, c.first_name, c.last_name, c.company, c.title,
       c.linkedin_url, c.linkedin_confidence, c.linkedin_status,
       g.name AS campaign_name
  FROM contacts c
  LEFT JOIN campaigns g ON g.id = c.campaign_id
 WHERE c.linkedin_url IS NOT NULL
   AND c.linkedin_status IN ('enriched', 'ready_to_draft', 'drafted')
   AND ($1::bigint IS NULL OR c.campaign_id = $1)
   AND NOT EXISTS (
       SELECT 1 FROM events e
        WHERE e.contact_id = c.id
          AND e.event_type IN ('enrichment_verified', 'enrichment_rejected')
   )
 ORDER BY c.linkedin_confidence ASC NULLS FIRST
 LIMIT $2;
"""


async def enrichment_review_queue(campaign_id: int | None = None,
                                  limit: int = 100) -> list[dict]:
    rows = await pool().fetch(ENRICHMENT_REVIEW_QUEUE_SQL, campaign_id, limit)
    return [dict(r) for r in rows]


CONFIRM_ENRICHMENT_SQL = """
INSERT INTO events (contact_id, channel, event_type, payload)
SELECT c.id, 'linkedin', 'enrichment_verified',
       json_build_object('confidence', c.linkedin_confidence,
                         'url', c.linkedin_url,
                         'reviewed_by', $2::text,
                         'reason', $3::text)::jsonb
  FROM contacts c
 WHERE c.id = $1 AND c.linkedin_url IS NOT NULL
RETURNING contact_id;
"""


async def confirm_enrichment(contact_id: int, reviewed_by: str,
                             reason: str | None = None) -> bool:
    # reason is the AI verifier's audit note ("AI: right_person (0.9) — …");
    # None for a human click.
    row = await pool().fetchrow(CONFIRM_ENRICHMENT_SQL, contact_id, reviewed_by, reason)
    return row is not None


# "Wrong person" is one atomic statement: the URL, its confidence, any
# scraped profile AND any draft personalised to that wrong profile all
# go together, and the contact re-enters the queue at ready_to_draft
# (email-only path — re-enriching would find the same wrong match).
# In-flight claims (enriching/scraping/drafting) are never touched.
REJECT_ENRICHMENT_SQL = """
WITH before AS (
    SELECT id, linkedin_url, linkedin_confidence
      FROM contacts
     WHERE id = $1
       AND linkedin_status IN ('enriched', 'ready_to_draft', 'drafted')
       FOR UPDATE
),
cleared AS (
    UPDATE contacts c
       SET linkedin_url        = NULL,
           linkedin_confidence = 0,
           profile_data        = NULL,
           profile_scraped_at  = NULL,
           apify_run_id        = NULL,
           email_subject       = NULL,
           email_body          = NULL,
           linkedin_note       = NULL,
           linkedin_status     = 'ready_to_draft',
           review_status       = 'pending_review',
           reviewed_at         = NULL,
           reviewed_by         = NULL,
           email_status        = CASE WHEN c.email_status = 'drafted'
                                      THEN 'pending' ELSE c.email_status END,
           last_action_at      = now()
      FROM before b
     WHERE c.id = b.id
    RETURNING c.id
)
INSERT INTO events (contact_id, channel, event_type, payload)
SELECT b.id, 'linkedin', 'enrichment_rejected',
       json_build_object('confidence', b.linkedin_confidence,
                         'url', b.linkedin_url,
                         'reviewed_by', $2::text,
                         'reason', $3::text)::jsonb
  FROM before b
  JOIN cleared ON cleared.id = b.id
RETURNING contact_id;
"""


async def reject_enrichment(contact_id: int, reviewed_by: str,
                            reason: str | None = None) -> bool:
    row = await pool().fetchrow(REJECT_ENRICHMENT_SQL, contact_id, reviewed_by, reason)
    return row is not None


VERIFICATION_OUTCOMES_SQL = """
SELECT event_type, count(*)::int AS n,
       round(avg((payload->>'confidence')::numeric), 2) AS avg_confidence,
       round(min((payload->>'confidence')::numeric), 2) AS min_confidence
  FROM events
 WHERE event_type IN ('enrichment_verified', 'enrichment_rejected')
 GROUP BY event_type;
"""


async def verification_outcomes() -> list[dict]:
    return [dict(r) for r in await pool().fetch(VERIFICATION_OUTCOMES_SQL)]


# The AI verify stage's work-list: same "no verdict yet" predicate as the
# human queue, but only PROFILE-BEARING contacts (a match with no scraped
# profile is template-drafted regardless, so a verdict changes nothing).
# exclude_ids are contacts already attempted this run — since a verdict is
# an event, not a status flip, this is what guarantees the runner's loop
# makes forward progress and terminates.
VERIFY_QUEUE_SQL = """
SELECT c.id, c.email, c.first_name, c.last_name, c.company, c.title,
       c.linkedin_url, c.profile_data
  FROM contacts c
 WHERE c.linkedin_url IS NOT NULL
   AND c.profile_data IS NOT NULL
   AND c.linkedin_status IN ('enriched', 'ready_to_draft', 'drafted')
   AND NOT (c.id = ANY($1::bigint[]))
   AND NOT EXISTS (
       SELECT 1 FROM events e
        WHERE e.contact_id = c.id
          AND e.event_type IN ('enrichment_verified', 'enrichment_rejected')
   )
 ORDER BY c.linkedin_confidence ASC NULLS FIRST
 LIMIT $2;
"""


async def verify_queue_batch(exclude_ids: list[int],
                             limit: int | None = None) -> list[VerifyTarget]:
    rows = await pool().fetch(
        VERIFY_QUEUE_SQL, exclude_ids or [], limit or config.VERIFY_BATCH_SIZE)
    return [
        VerifyTarget(
            id=r["id"], email=r["email"],
            first_name=r["first_name"], last_name=r["last_name"],
            company=r["company"], title=r["title"],
            linkedin_url=r["linkedin_url"], profile_data=_jsonb(r["profile_data"]),
        )
        for r in rows
    ]


# The verify page's audit list: the latest verdicts (AI or human) with the
# reason and reviewer, so the operator can spot-check what the AI decided.
RECENT_VERIFICATIONS_SQL = """
SELECT e.id, e.contact_id, e.event_type, e.created_at,
       e.payload->>'reviewed_by' AS reviewed_by,
       e.payload->>'reason'      AS reason,
       e.payload->>'confidence'  AS confidence,
       c.first_name, c.last_name, c.company, c.email,
       c.linkedin_url, c.linkedin_status
  FROM events e
  LEFT JOIN contacts c ON c.id = e.contact_id
 WHERE e.event_type IN ('enrichment_verified', 'enrichment_rejected')
 ORDER BY e.id DESC
 LIMIT $1;
"""


async def recent_verifications(limit: int = 20) -> list[dict]:
    return [dict(r) for r in await pool().fetch(RECENT_VERIFICATIONS_SQL, limit)]


# =====================================================================
# ui: draft review
# =====================================================================

# linkedin_status is selected because the review card template branches
# on it — omitting it rendered every page-load card as "no longer in the
# drafted state" (live bug, 2026-08-11).
REVIEW_QUEUE_SQL = """
SELECT c.id, c.email, c.first_name, c.last_name, c.company, c.title,
       c.email_subject, c.email_body, c.linkedin_note, c.linkedin_url,
       c.linkedin_status, c.review_status, c.reviewed_at, c.reviewed_by,
       c.profile_data, g.name AS campaign_name
  FROM contacts c
  LEFT JOIN campaigns g ON g.id = c.campaign_id
 WHERE c.linkedin_status = 'drafted'
   AND c.review_status = $1
   AND ($2::bigint IS NULL OR c.campaign_id = $2)
 ORDER BY c.created_at
 LIMIT $3;
"""


async def review_queue(review_status: str = "pending_review",
                       campaign_id: int | None = None,
                       limit: int = 50) -> list[dict]:
    rows = await pool().fetch(REVIEW_QUEUE_SQL, review_status, campaign_id, limit)
    result = []
    for r in rows:
        item = dict(r)
        item["profile_data"] = _jsonb(item["profile_data"])
        result.append(item)
    return result


CONTACT_DETAIL_SQL = """
SELECT c.*, g.name AS campaign_name
  FROM contacts c
  LEFT JOIN campaigns g ON g.id = c.campaign_id
 WHERE c.id = $1;
"""


async def contact_detail(contact_id: int) -> dict | None:
    row = await pool().fetchrow(CONTACT_DETAIL_SQL, contact_id)
    if row is None:
        return None
    item = dict(row)
    item["profile_data"] = _jsonb(item["profile_data"])
    return item


# Guarded on 'drafted' so a human edit can never race a concurrent
# re-draft of the same contact.
UPDATE_DRAFT_SQL = """
WITH updated AS (
    UPDATE contacts
       SET email_subject  = $2,
           email_body     = $3,
           linkedin_note  = $4,
           last_action_at = now()
     WHERE id = $1 AND linkedin_status = 'drafted'
    RETURNING id
)
INSERT INTO events (contact_id, channel, event_type, payload)
SELECT id, 'email', 'draft_edited',
       json_build_object('by', $5::text)::jsonb
  FROM updated
RETURNING contact_id;
"""


async def update_draft(contact_id: int, subject: str, body: str,
                       note: str | None, by: str) -> bool:
    row = await pool().fetchrow(UPDATE_DRAFT_SQL, contact_id, subject, body, note, by)
    return row is not None


SET_REVIEW_STATUS_SQL = """
WITH updated AS (
    UPDATE contacts
       SET review_status  = $2,
           reviewed_at    = now(),
           reviewed_by    = $3,
           last_action_at = now()
     WHERE id = $1 AND linkedin_status = 'drafted'
    RETURNING id
)
INSERT INTO events (contact_id, channel, event_type, payload)
SELECT id, 'email',
       CASE WHEN $2 = 'approved' THEN 'draft_approved' ELSE 'draft_rejected' END,
       json_build_object('by', $3::text)::jsonb
  FROM updated
RETURNING contact_id;
"""


async def set_review_status(contact_id: int, status: str, by: str) -> bool:
    row = await pool().fetchrow(SET_REVIEW_STATUS_SQL, contact_id, status, by)
    return row is not None


REQUEUE_REDRAFT_SQL = """
WITH updated AS (
    UPDATE contacts
       SET linkedin_status = 'ready_to_draft',
           review_status   = 'pending_review',
           reviewed_at     = NULL,
           reviewed_by     = NULL,
           last_action_at  = now()
     WHERE id = $1 AND linkedin_status = 'drafted'
    RETURNING id
)
INSERT INTO events (contact_id, channel, event_type, payload)
SELECT id, 'email', 'redraft_requested',
       json_build_object('by', $2::text)::jsonb
  FROM updated
RETURNING contact_id;
"""


async def requeue_for_redraft(contact_id: int, by: str) -> bool:
    row = await pool().fetchrow(REQUEUE_REDRAFT_SQL, contact_id, by)
    return row is not None


REVIEW_COUNTS_SQL = """
SELECT review_status, count(*)::int AS n
  FROM contacts
 WHERE linkedin_status = 'drafted'
 GROUP BY review_status;
"""


async def review_counts() -> dict[str, int]:
    rows = await pool().fetch(REVIEW_COUNTS_SQL)
    return {r["review_status"]: r["n"] for r in rows}


# =====================================================================
# ui: dashboard + campaigns
# =====================================================================

# Ordered by id: id order is insertion order and created_at (which the
# dashboard displays) is guaranteed by migration 006, which owns this table.
RECENT_EVENTS_SQL = """
SELECT e.id, e.contact_id, e.channel, e.event_type, e.payload, e.created_at,
       c.email
  FROM events e
  LEFT JOIN contacts c ON c.id = e.contact_id
 WHERE ($1::boolean IS FALSE
        OR e.event_type LIKE '%\\_failed'
        OR e.event_type LIKE '%\\_rejected')
 ORDER BY e.id DESC
 LIMIT $2;
"""


async def recent_events(limit: int = 30, only_errors: bool = False) -> list[dict]:
    rows = await pool().fetch(RECENT_EVENTS_SQL, only_errors, limit)
    result = []
    for r in rows:
        item = dict(r)
        item["payload"] = _jsonb(item["payload"])
        result.append(item)
    return result


# The dashboard's "in queue" KPI: same predicate as the verify queue,
# but a count — the page must not pay for (or cap at) the row fetch.
COUNT_VERIFY_QUEUE_SQL = """
SELECT count(*)::int AS n
  FROM contacts c
 WHERE c.linkedin_url IS NOT NULL
   AND c.linkedin_status IN ('enriched', 'ready_to_draft', 'drafted')
   AND NOT EXISTS (SELECT 1 FROM events e
                    WHERE e.contact_id = c.id
                      AND e.event_type IN ('enrichment_verified', 'enrichment_rejected'));
"""


async def count_verify_queue() -> int:
    return await pool().fetchval(COUNT_VERIFY_QUEUE_SQL)


# Error share of the last 24h of events ("_rejected" excluded: a human
# verdict is an outcome, not a failure).
EVENTS_ERROR_STATS_24H_SQL = r"""
SELECT count(*) FILTER (WHERE event_type LIKE '%\_failed')::int AS errors,
       count(*)::int AS total
  FROM events
 WHERE created_at > now() - interval '24 hours';
"""


async def events_error_stats_24h() -> tuple[int, int]:
    row = await pool().fetchrow(EVENTS_ERROR_STATS_24H_SQL)
    return row["errors"], row["total"]


LIST_CAMPAIGNS_SQL = """
SELECT g.id, g.name, g.status, g.consent_status, g.created_at,
       count(c.id)::int AS contacts,
       count(*) FILTER (WHERE c.linkedin_status = 'drafted')::int AS drafted,
       count(*) FILTER (WHERE c.linkedin_status = 'drafted'
                          AND c.review_status = 'approved')::int AS approved
  FROM campaigns g
  LEFT JOIN contacts c ON c.campaign_id = g.id
 GROUP BY g.id
 ORDER BY g.created_at DESC;
"""


async def list_campaigns() -> list[dict]:
    return [dict(r) for r in await pool().fetch(LIST_CAMPAIGNS_SQL)]


async def get_campaign(campaign_id: int) -> dict | None:
    row = await pool().fetchrow("SELECT * FROM campaigns WHERE id = $1;", campaign_id)
    return dict(row) if row else None


CAMPAIGN_FIELDS = (
    "name", "status", "offer_description", "cta", "tone", "sender_name",
    "sender_role", "audience_rationale", "consent_status", "channel_policy",
    "fallback_email_subject", "fallback_email_body",
    # migration 005: per-campaign send configuration
    "sender_email", "smartlead_campaign_id",
)

# Everything the edit form owns — i.e. CAMPAIGN_FIELDS minus
# smartlead_campaign_id. That column has a second, non-human writer
# (set_smartlead_campaign_id, from the Smartlead auto-setup), so a
# full-row update let a form rendered BEFORE setup ran write its stale
# empty value back over a freshly created id, silently making a cold
# campaign unsendable. Creation still sets the column (as NULL).
CAMPAIGN_UPDATE_FIELDS = tuple(
    f for f in CAMPAIGN_FIELDS if f != "smartlead_campaign_id"
)

CREATE_CAMPAIGN_SQL = f"""
INSERT INTO campaigns ({", ".join(CAMPAIGN_FIELDS)})
VALUES ({", ".join(f"${i + 1}" for i in range(len(CAMPAIGN_FIELDS)))})
RETURNING id;
"""

UPDATE_CAMPAIGN_SQL = f"""
UPDATE campaigns
   SET {", ".join(f"{f} = ${i + 2}" for i, f in enumerate(CAMPAIGN_UPDATE_FIELDS))}
 WHERE id = $1
RETURNING id;
"""


async def create_campaign(fields: dict) -> int:
    row = await pool().fetchrow(
        CREATE_CAMPAIGN_SQL, *[fields.get(f) for f in CAMPAIGN_FIELDS]
    )
    return row["id"]


async def update_campaign(campaign_id: int, fields: dict) -> bool:
    """Writes CAMPAIGN_UPDATE_FIELDS only; smartlead_campaign_id is not
    the form's to set (see set_smartlead_campaign_id)."""
    row = await pool().fetchrow(
        UPDATE_CAMPAIGN_SQL, campaign_id,
        *[fields.get(f) for f in CAMPAIGN_UPDATE_FIELDS]
    )
    return row is not None


async def set_smartlead_campaign_id(campaign_id: int, smartlead_id: str) -> bool:
    """Single-column write for the auto-setup path — a full
    update_campaign here would race concurrent edits of the other
    thirteen fields."""
    row = await pool().fetchrow(
        "UPDATE campaigns SET smartlead_campaign_id = $2 WHERE id = $1 RETURNING id;",
        campaign_id, smartlead_id,
    )
    return row is not None


# text[] round-trips through asyncpg as a Python list with no codec; NULL
# means "all connected mailboxes". Kept out of CAMPAIGN_UPDATE_FIELDS (the
# scalar form-string machinery) and written on its own, like
# smartlead_campaign_id — the mailbox selection is an array and has its
# own edit-page action.
SET_CAMPAIGN_MAILBOXES_SQL = (
    "UPDATE campaigns SET smartlead_mailboxes = $2 WHERE id = $1 RETURNING id;"
)


async def set_campaign_mailboxes(campaign_id: int,
                                 emails: list[str] | None) -> bool:
    """Persist the campaign's send-from mailbox selection (a list of
    addresses), or NULL for 'all'. An empty list is normalised to NULL so
    'no selection' has one representation."""
    row = await pool().fetchrow(
        SET_CAMPAIGN_MAILBOXES_SQL, campaign_id, emails or None,
    )
    return row is not None


async def delete_campaign(campaign_id: int) -> bool:
    """Delete a campaign and everything it owns LOCALLY — its contacts
    (and, via ON DELETE CASCADE, those contacts' events). Returns False
    if no such campaign existed.

    Contacts are removed explicitly rather than trusting a
    campaigns→contacts cascade: the base migration that would declare it
    is not in this repo (see migration 006's note), so its FK behaviour
    is unknown. Deleting children first is correct whether or not the
    cascade exists. suppression is deliberately NOT touched — it is a
    global do-not-contact list keyed by email, not campaign data;
    dropping a test campaign must never resurrect a suppressed address.
    The Smartlead campaign is left alone too: this removes our record,
    not the vendor's."""
    async with pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM contacts WHERE campaign_id = $1;", campaign_id
            )
            row = await conn.fetchrow(
                "DELETE FROM campaigns WHERE id = $1 RETURNING id;", campaign_id
            )
    return row is not None


# =====================================================================
# shared
# =====================================================================


async def status_counts() -> dict[str, int]:
    rows = await pool().fetch(
        "SELECT linkedin_status, count(*)::int AS n FROM contacts GROUP BY linkedin_status;"
    )
    return {r["linkedin_status"]: r["n"] for r in rows}
