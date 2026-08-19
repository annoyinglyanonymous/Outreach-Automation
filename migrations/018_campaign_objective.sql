-- 018: store the campaign objective as the per-campaign drafting prompt.
--
-- The objective used to be expanded into the brief at creation and discarded.
-- It is now STORED and becomes the campaign's drafting instructions: when set,
-- drafting.build_prompts uses it as the system prompt, wrapped with a fixed
-- scaffold of mechanical rules the pipeline depends on (blank-line paragraphs,
-- anchor-tag links only, personalize-only-from-data, no sign-off — the
-- signature is appended automatically). Editable on the campaign page, so
-- prompt iteration is: edit objective -> send test email -> release. No code
-- change per campaign.
--
-- Deliberately NULLABLE with no default: NULL/empty means "use the built-in
-- default prompt" (the Agency Value Calculator prompt in app/drafting.py), so
-- every existing campaign keeps today's behaviour untouched until an operator
-- writes an objective.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS; re-running is a no-op.

ALTER TABLE campaigns
    ADD COLUMN IF NOT EXISTS objective text;

-- Verification: must return a single row with objective_ok = true.
SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_name = 'campaigns' AND column_name = 'objective'
) AS objective_ok;
