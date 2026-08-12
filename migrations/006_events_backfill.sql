-- 006: backfill base-schema pieces the live database turned out to lack.
--
-- The original base migration (001) never made it into this repo — the
-- same botched transfer that mangled the Python files — and the live
-- schema (n8n era) was missing the events table entirely, found via
-- /ui/verify: relation "events" does not exist. Every stage writes
-- events (enrichment outcomes, verify/review verdicts, email_sent), so
-- nothing that logs can run without it.
--
-- Also guarded here, same risk class, cheap if already present:
-- - suppression: the email claim and pre-send sweep join on
--   lower(email); the table must exist even if empty.
-- - contacts.linkedin_confidence: written by enrichment, read by the
--   verify queue's ordering — the one contacts column no earlier
--   migration's verification checked.
--
-- Idempotent throughout: IF NOT EXISTS everywhere.

CREATE TABLE IF NOT EXISTS events (
    id          bigserial PRIMARY KEY,
    contact_id  bigint REFERENCES contacts(id) ON DELETE CASCADE,
    channel     text,
    event_type  text NOT NULL,
    payload     jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- The verify queue probes NOT EXISTS per (contact, event_type); the
-- recent-events feed reads newest-first by id (covered by the PK).
CREATE INDEX IF NOT EXISTS events_contact_type_idx
    ON events (contact_id, event_type);

CREATE TABLE IF NOT EXISTS suppression (
    id          bigserial PRIMARY KEY,
    email       text NOT NULL,
    reason      text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Case-insensitive dedupe: all suppression joins are on lower(email).
CREATE UNIQUE INDEX IF NOT EXISTS suppression_email_lower_idx
    ON suppression (lower(email));

ALTER TABLE contacts ADD COLUMN IF NOT EXISTS linkedin_confidence numeric;

-- Verification: must return a single row with base_ok = true.
SELECT to_regclass('public.events')      IS NOT NULL
   AND to_regclass('public.suppression') IS NOT NULL
   AND EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'contacts'
                  AND column_name = 'linkedin_confidence')
       AS base_ok;
