-- 003: drafting stage.
--
-- Adds the 'drafting' in-progress state to the linkedin_status CHECK.
-- Every stage needs its own in-progress status: it is what makes
-- FOR UPDATE SKIP LOCKED claims crash-recoverable (reset_stale_* finds
-- them) and prevents two runners double-claiming the same contact.
--
-- The constraint is rewritten from its live definition rather than
-- re-enumerated by hand, so any status values this file's author did
-- not know about are preserved. Idempotent: re-running is a no-op.

DO $$
DECLARE
    con name;
    def text;
BEGIN
    SELECT conname, pg_get_constraintdef(oid)
      INTO con, def
      FROM pg_constraint
     WHERE conrelid = 'contacts'::regclass
       AND contype = 'c'
       AND pg_get_constraintdef(oid) LIKE '%linkedin_status%';

    IF con IS NULL THEN
        RAISE EXCEPTION 'no linkedin_status CHECK constraint found on contacts';
    END IF;

    IF def LIKE '%''drafting''%' THEN
        RAISE NOTICE 'drafting already allowed, nothing to do';
        RETURN;
    END IF;

    def := replace(def, 'ARRAY[', 'ARRAY[''drafting''::text, ');

    EXECUTE format('ALTER TABLE contacts DROP CONSTRAINT %I', con);
    EXECUTE format('ALTER TABLE contacts ADD CONSTRAINT %I %s', con, def);
END $$;

-- Verification: must return a single row with drafting_allowed = true.
SELECT pg_get_constraintdef(oid) LIKE '%''drafting''%' AS drafting_allowed
  FROM pg_constraint
 WHERE conrelid = 'contacts'::regclass
   AND contype = 'c'
   AND pg_get_constraintdef(oid) LIKE '%linkedin_status%';
