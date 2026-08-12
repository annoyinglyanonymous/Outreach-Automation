-- 008: maintain contacts.updated_at.
--
-- Confirmed against the live database (2026-08-12): contacts has NO user
-- triggers, and updated_at carries only its DEFAULT now() from insert — so
-- it never advances. Every query that reads it as "when was this row last
-- touched" has been comparing against creation time instead:
--
-- - reset_stale_claims / reset_stale_scrape_claims / reset_stale_draft_claims
--   intend a STALE_CLAIM_MINUTES grace period before returning a claimed row
--   to the queue. With updated_at frozen at insert, any contact ingested
--   longer ago than that window is eligible the instant it is claimed — the
--   grace period does not exist. Two app instances would therefore un-claim
--   each other's live batches immediately rather than after 15 minutes, and
--   re-pay the vendor for the overlap.
-- - count_stuck_sending reports every contact at 'sending' that was created
--   more than STALE_CLAIM_MINUTES ago — during a normal send run, all of
--   them. That is a false "stuck, resolve by hand against the provider
--   dashboard" alarm on the dashboard and in /stats, raised against rows
--   that are merely in flight. Acting on it risks a double send.
--
-- Evidence at the time of writing: two rows already carry last_action_at
-- AHEAD of updated_at, the worst by ~21 hours (contact 7).
--
-- Side effect, accepted deliberately: CACHE_SQL treats updated_at as the age
-- of the cached linkedin_url. With the trigger, a later stage touching the
-- row (drafting, sending) also refreshes it, so a cache entry can outlive
-- CACHE_MAX_AGE_DAYS from when the URL was actually written. At 365 days the
-- practical effect is negligible; tighten by adding a dedicated
-- linkedin_resolved_at column if that ever stops being true.
--
-- Existing rows are NOT backfilled: updated_at is only meaningful from here
-- on, and rewriting history would itself be a mass update.
--
-- Idempotent: CREATE OR REPLACE on the function, DROP IF EXISTS on the
-- trigger before recreating it.

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS contacts_set_updated_at ON contacts;

CREATE TRIGGER contacts_set_updated_at
    BEFORE UPDATE ON contacts
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- Verification: must return a single row with updated_at_ok = true.
SELECT EXISTS (
    SELECT 1
      FROM pg_trigger
     WHERE tgrelid = 'contacts'::regclass
       AND tgname = 'contacts_set_updated_at'
       AND NOT tgisinternal
) AS updated_at_ok;
