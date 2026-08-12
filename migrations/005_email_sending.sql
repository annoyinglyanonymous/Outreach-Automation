-- 005: email sending stage.
--
-- campaigns.smartlead_campaign_id — the Smartlead campaign that delivers
--   this campaign's cold sends. Its sequence must be a bare shell of the
--   merge variables {{personalized_subject}} / {{personalized_body}}; we
--   push each approved contact as a lead carrying those custom fields.
--   NULL means "cold sends not configured" and the claim skips the
--   campaign rather than failing the run.
-- campaigns.sender_email — the From address for opted-in (transactional)
--   sends. Same NULL semantics.
-- contacts.email_sent_at — when the send was handed to delivery
--   infrastructure (Smartlead schedules actual delivery itself).
--
-- The email_status CHECK gains 'sending' (in-flight claim), 'sent_email'
-- (handed off), 'failed' (hard rejection) and 'suppressed' (compliance)
-- if missing — each injected individually so any values this file's
-- author did not know about are preserved. Idempotent throughout.

ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS smartlead_campaign_id text;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS sender_email text;
ALTER TABLE contacts  ADD COLUMN IF NOT EXISTS email_sent_at timestamptz;

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

    FOREACH val IN ARRAY ARRAY['sending', 'sent_email', 'failed', 'suppressed'] LOOP
        IF def NOT LIKE '%''' || val || '''%' THEN
            def := replace(def, 'ARRAY[', 'ARRAY[''' || val || '''::text, ');
            changed := true;
        END IF;
    END LOOP;

    IF NOT changed THEN
        RAISE NOTICE 'email_status already allows all send states';
        RETURN;
    END IF;

    EXECUTE format('ALTER TABLE contacts DROP CONSTRAINT %I', con);
    EXECUTE format('ALTER TABLE contacts ADD CONSTRAINT %I %s', con, def);
END $$;

-- Verification: must return a single row with email_ok = true.
SELECT (SELECT count(*)::int FROM information_schema.columns
         WHERE table_name = 'campaigns'
           AND column_name IN ('smartlead_campaign_id', 'sender_email')) = 2
   AND EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'contacts' AND column_name = 'email_sent_at')
   AND (SELECT bool_and(c.def LIKE '%''' || v.val || '''%')
          FROM (SELECT pg_get_constraintdef(oid) AS def
                  FROM pg_constraint
                 WHERE conrelid = 'contacts'::regclass AND contype = 'c'
                   AND pg_get_constraintdef(oid) LIKE '%email_status%') c,
               unnest(ARRAY['sending', 'sent_email', 'failed', 'suppressed']) AS v(val))
       AS email_ok;
