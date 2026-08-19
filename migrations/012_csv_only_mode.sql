-- 012: per-campaign "CSV-only" mode — skip Apollo/Apify/verify and draft a
-- personalized email straight from the uploaded sheet.
--
-- For large lists (~12k) LinkedIn find + scrape isn't worth the per-contact
-- cost. A campaign in 'csv' mode has its contacts INSERTed directly by the
-- app (the only path that does — the 'linkedin' mode still ingests via the
-- n8n webhook), landing them at linkedin_status = 'ready_to_draft' so the
-- global enrich/scrape/verify claims (which key off 'pending'/'enriched'/a
-- non-null linkedin_url) never touch them, and the drafter personalizes from
-- the sheet columns instead of a scraped LinkedIn profile.
--
-- campaigns.enrichment_mode: 'linkedin' (default — the full pipeline, exactly
-- as before) or 'csv'. A CHECK'd text enum, not a boolean, so a future mode
-- (e.g. a no-LLM variant) is additive — same modelling as channel_policy /
-- email_status. NOT NULL DEFAULT 'linkedin' backfills every existing row, so
-- no campaign changes behaviour.
--
-- contacts.extra_data: the arbitrary sheet columns captured at CSV-only
-- ingest (notes / website / state / revenue / …), keyed by original header.
-- jsonb, parallel to profile_data, but written ONLY by the CSV-only insert —
-- deliberately NOT profile_data, whose presence is the LLM-vs-template signal
-- in drafting and which the scrape/reject paths own.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS. The base contacts/campaigns DDL is
-- out-of-repo (Supabase) — these only ALTER existing tables (see 006).

ALTER TABLE campaigns
    ADD COLUMN IF NOT EXISTS enrichment_mode text NOT NULL DEFAULT 'linkedin'
        CHECK (enrichment_mode IN ('linkedin', 'csv'));

ALTER TABLE contacts
    ADD COLUMN IF NOT EXISTS extra_data jsonb;

-- Verification: must return a single row with csv_mode_ok = true.
SELECT (
    EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'campaigns' AND column_name = 'enrichment_mode')
    AND
    EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'contacts' AND column_name = 'extra_data')
) AS csv_mode_ok;
