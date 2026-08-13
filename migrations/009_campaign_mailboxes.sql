-- 009: per-campaign Smartlead mailbox selection.
--
-- Until now the Smartlead auto-setup (app/providers/smartlead.py
-- setup_campaign) attached EVERY connected mailbox to every new campaign.
-- That gives no deliverability isolation (a cold blast rotates through
-- inboxes used for warm replies), no persona/domain separation, and drags
-- warmup-only inboxes into live sends.
--
-- This column stores the operator's chosen send-from mailboxes as their
-- EMAIL addresses (not Smartlead account ids, which are opaque and change
-- when a mailbox is disconnected and re-added). The addresses are resolved
-- to live account ids against GET /email-accounts/ at attach time.
--
-- NULL (the default, and every existing row) means "attach all connected
-- mailboxes" — the prior behaviour — so this migration changes nothing on
-- its own. An empty array is never written: the app stores NULL for
-- "no selection" so the two cannot diverge.
--
-- text[], not jsonb: asyncpg round-trips a Python list <-> text[] natively
-- (the codebase already passes list params as $1::text[] throughout), so
-- no json codec or json.dumps/loads is needed on read or write.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS.

ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS smartlead_mailboxes text[];

-- Verification: must return a single row with mailboxes_column_ok = true.
SELECT EXISTS (
    SELECT 1
      FROM information_schema.columns
     WHERE table_name = 'campaigns'
       AND column_name = 'smartlead_mailboxes'
) AS mailboxes_column_ok;
