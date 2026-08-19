-- 017: per-campaign test-email gate.
--
-- A campaign can't send real emails until its test has been approved: send a
-- test copy to an inbox you control, eyeball it, then release. The gate is
-- enforced in CLAIM_EMAIL_SQL (so the drip AND the "send approved now"
-- override both respect it) and surfaced by unsendable_approved_counts.
--
-- States: 'pending' (default for NEW campaigns) -> 'sent' (a test went out,
-- awaiting approval) -> 'approved' (releases sending). Existing rows are
-- backfilled to 'approved' so nothing already in flight halts on apply.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS (nullable first, so the CHECK accepts
-- the NULL existing rows briefly); the backfill only touches NULLs; DEFAULT and
-- NOT NULL are set after. Re-running is a no-op.

ALTER TABLE campaigns
    ADD COLUMN IF NOT EXISTS test_status text
        CHECK (test_status IN ('pending', 'sent', 'approved'));

-- Existing campaigns: released (don't halt in-flight sends).
UPDATE campaigns SET test_status = 'approved' WHERE test_status IS NULL;

-- Future campaigns: gated until tested.
ALTER TABLE campaigns ALTER COLUMN test_status SET DEFAULT 'pending';
ALTER TABLE campaigns ALTER COLUMN test_status SET NOT NULL;

-- Verification: must return a single row with test_gate_ok = true.
SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_name = 'campaigns' AND column_name = 'test_status'
) AS test_gate_ok;
