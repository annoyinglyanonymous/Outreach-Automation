-- 004: human review gate for drafts.
--
-- review_status is deliberately a separate column, not a new
-- linkedin_status value: the channel status columns mean "where in the
-- pipeline" and are what FOR UPDATE SKIP LOCKED claims key on, while
-- approval is a human judgement about content. Keeping them orthogonal
-- means the queue semantics are untouched and future send stages simply
-- filter  WHERE linkedin_status = 'drafted' AND review_status = 'approved'.
--
-- The default is meaningful only once a contact reaches 'drafted';
-- before that it is harmless. A re-draft resets it in code
-- (WRITE_DRAFTS_SQL), so an approval can never outlive the copy it
-- approved.
--
-- Idempotent: IF NOT EXISTS on every column; the CHECK rides on the
-- column definition so it is only created with it. Constant default is
-- metadata-only on Postgres 11+ (no table rewrite).

ALTER TABLE contacts
    ADD COLUMN IF NOT EXISTS review_status text NOT NULL DEFAULT 'pending_review'
        CHECK (review_status IN ('pending_review', 'approved', 'rejected'));

ALTER TABLE contacts
    ADD COLUMN IF NOT EXISTS reviewed_at timestamptz;

ALTER TABLE contacts
    ADD COLUMN IF NOT EXISTS reviewed_by text;

-- Verification: must return a single row with all three columns listed
-- and review_ok = true.
SELECT (SELECT count(*)::int FROM information_schema.columns
         WHERE table_name = 'contacts'
           AND column_name IN ('review_status', 'reviewed_at', 'reviewed_by')) = 3
       AND EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'contacts'::regclass AND contype = 'c'
                      AND pg_get_constraintdef(oid) LIKE '%review_status%')
       AS review_ok;
