-- 016: per-campaign send mode — how an approved contact leaves the queue.
--
-- 'batch' (default): the business-hours drip. emailer.run() sends one batch
-- (one email per active mailbox) per 5-minute scheduler tick, only inside
-- SEND_WINDOW_* hours. The steady, deliverability-safe default; every existing
-- campaign keeps exactly this behaviour.
-- 'immediate': on approval the campaign's whole approved-but-unsent queue is
-- drained at once (emailer.send_campaign_now), bypassing the send window AND
-- the drip pacing — for warming a mailbox or a small urgent batch. Every other
-- guarantee still holds: suppression sweep, one-first-touch claim, the sender
-- allowlist/rotation, and the appended signature.
--
-- A CHECK'd text enum, not a boolean, so a third mode stays additive — same
-- modelling as channel_policy / enrichment_mode. NOT NULL DEFAULT 'batch'
-- backfills every existing row, so no campaign changes behaviour on apply.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS. The base campaigns DDL is out-of-repo
-- (Supabase); this only ALTERs the existing table (see 006).

ALTER TABLE campaigns
    ADD COLUMN IF NOT EXISTS send_mode text NOT NULL DEFAULT 'batch'
        CHECK (send_mode IN ('batch', 'immediate'));

-- Verification: must return a single row with send_mode_ok = true.
SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_name = 'campaigns' AND column_name = 'send_mode'
) AS send_mode_ok;
