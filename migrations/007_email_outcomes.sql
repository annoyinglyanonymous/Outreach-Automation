-- 007: reply & bounce outcome states for email_status.
--
-- The Smartlead webhook (docs/n8n-suppression-webhook.json) writes these
-- back from live sends: EMAIL_REPLY -> 'replied', a hard EMAIL_BOUNCE ->
-- 'bounced' (and also suppresses the address). Migration 005 added the
-- send states ('sending', 'sent_email', 'failed', 'suppressed') but not
-- these two outcomes, so the dashboard's reply-rate and bounce KPIs read
-- columns nothing could legally write. Add them exactly as 005 did —
-- inject each value individually so any values not known here survive.
--
-- Idempotent: re-running is a no-op once both values are present.

DO $$
DECLARE
    con name;
    def text;
    val text;
    changed boolean := false;
BEGIN
    SELECT conname, pg_get_constraintdef(oid)
      INTO con, def
      FROM pg_constraint
     WHERE conrelid = 'contacts'::regclass
       AND contype = 'c'
       AND pg_get_constraintdef(oid) LIKE '%email_status%';

    IF con IS NULL THEN
        RAISE EXCEPTION 'no email_status CHECK constraint found on contacts';
    END IF;

    FOREACH val IN ARRAY ARRAY['replied', 'bounced'] LOOP
        IF def NOT LIKE '%''' || val || '''%' THEN
            def := replace(def, 'ARRAY[', 'ARRAY[''' || val || '''::text, ');
            changed := true;
        END IF;
    END LOOP;

    IF NOT changed THEN
        RAISE NOTICE 'email_status already allows replied/bounced';
        RETURN;
    END IF;

    EXECUTE format('ALTER TABLE contacts DROP CONSTRAINT %I', con);
    EXECUTE format('ALTER TABLE contacts ADD CONSTRAINT %I %s', con, def);
END $$;

-- Verification: must return a single row with email_outcomes_ok = true.
SELECT (SELECT bool_and(c.def LIKE '%''' || v.val || '''%')
          FROM (SELECT pg_get_constraintdef(oid) AS def
                  FROM pg_constraint
                 WHERE conrelid = 'contacts'::regclass AND contype = 'c'
                   AND pg_get_constraintdef(oid) LIKE '%email_status%') c,
               unnest(ARRAY['replied', 'bounced']) AS v(val))
       AS email_outcomes_ok;
