-- 010: global sender-rotation pool for cold (Mailjet).
--
-- Cold now sends via Mailjet, a transactional ESP that pools everything under
-- its own IPs — so the one deliverability lever left is DOMAIN-reputation
-- distribution: rotate the From across many validated sending domains so no
-- single domain carries all the cold volume. This table is that pool.
--
-- One GLOBAL pool shared by every cold campaign (Renegade is one brand, and a
-- domain's reputation is account-level, not campaign-level; a shared pool also
-- keeps every domain sending steadily, which is what builds reputation). The
-- email runner picks the least-recently-used active sender with remaining daily
-- capacity, atomically, and stamps it as the From — see repo.claim_rotating_sender.
--
-- Per-domain daily cap needs durable, concurrency-safe state, hence a table (not
-- config): sent_today is a counter, day is the date it counts for. The counter
-- resets when the day rolls over — the reset is folded into the pick UPDATE
-- (CASE WHEN day < current_date THEN 1 ELSE sent_today + 1 END), so no cron.
--
-- Timezone: current_date / now() on Supabase Postgres are UTC, matching this
-- codebase's server-time convention (everything is now() + interval). So the
-- daily cap rolls over at UTC midnight, NOT US-business-day midnight — a
-- deliberate, acceptable simplification for a daily volume cap.
--
-- daily_cap defaults to 25 (the ramp's starting point for a fresh, unwarmed
-- domain; raised toward ~50 over the first weeks by editing the row). active is
-- the manual kill switch to pull a degrading domain from rotation.
--
-- Idempotent: CREATE TABLE / CREATE INDEX IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS mailjet_senders (
    id           bigserial PRIMARY KEY,
    sender_email text        NOT NULL,
    sender_name  text,
    active       boolean     NOT NULL DEFAULT true,
    daily_cap    int         NOT NULL DEFAULT 25,
    sent_today   int         NOT NULL DEFAULT 0,
    day          date        NOT NULL DEFAULT current_date,
    last_used_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Case-insensitive uniqueness: an address is one sending identity regardless of
-- how it's typed, and the pick/rotation reasons about it by lower(email).
CREATE UNIQUE INDEX IF NOT EXISTS mailjet_senders_email_lower_idx
    ON mailjet_senders (lower(sender_email));

-- Verification: must return a single row with senders_ok = true.
SELECT to_regclass('public.mailjet_senders') IS NOT NULL AS senders_ok;
