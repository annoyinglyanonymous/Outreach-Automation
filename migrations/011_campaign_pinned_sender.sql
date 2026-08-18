-- 011: per-campaign single-mailbox pin — an optional override of the cold
-- From-rotation (migration 010).
--
-- By default every cold send rotates its From across the whole verified
-- sender pool, to spread domain reputation. Some campaigns instead need to
-- send from ONE specific mailbox — e.g. a named-person sequence where every
-- touch must come from the same address. This column is that opt-in.
--
-- NULL (the default) = rotate across the pool, exactly as before. A non-NULL
-- id pins every send for the campaign to that one sender. The pin narrows
-- WHICH sender is used; it does NOT lift the per-domain daily cap — a pinned
-- campaign still stops at that mailbox's daily_cap (see repo.claim_pinned_sender
-- and the per-campaign capacity gate in CLAIM_EMAIL_SQL). "Money is bounded"
-- stays true.
--
-- ON DELETE SET NULL: hard-deleting a pinned sender reverts its campaigns to
-- pool rotation (the safe default keeps them sending rather than silently
-- stalling). The normal lifecycle is PAUSE (active = false), not delete: a
-- paused pin leaves the campaign unsendable and is surfaced by
-- unsendable_approved_counts, which honours "only from that mailbox".
--
-- Idempotent: ADD COLUMN IF NOT EXISTS. Requires migration 010 (the FK
-- target mailjet_senders) to have been applied first.

ALTER TABLE campaigns
    ADD COLUMN IF NOT EXISTS pinned_sender_id bigint
        REFERENCES mailjet_senders (id) ON DELETE SET NULL;

-- Verification: must return a single row with pinned_sender_ok = true.
SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_name = 'campaigns' AND column_name = 'pinned_sender_id'
) AS pinned_sender_ok;
