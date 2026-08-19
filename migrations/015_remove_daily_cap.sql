-- 015: remove the per-sender daily cap.
--
-- Sending is now a business-hours drip — one batch (one email per active
-- mailbox) per 5-minute scheduler tick, only inside SEND_WINDOW_* hours. That
-- cadence + time window is the throttle, so the per-sender daily cap is
-- retired. The claim SQL treats daily_cap <= 0 as "no cap"
-- (CLAIM_EMAIL_SQL / CLAIM_ROTATING_SENDER_SQL / CLAIM_PINNED_SENDER_SQL), so
-- zeroing the column lifts the ceiling without touching the columns or the
-- sent_today accounting (the Senders "Today" counter still works).
--
-- Idempotent: only rewrites rows that still carry a positive cap, and re-runs
-- are no-ops. An operator can still cap one mailbox later by setting a
-- positive daily_cap on the Senders page.

UPDATE mailjet_senders
   SET daily_cap = 0
 WHERE daily_cap <> 0;

-- Verification: must return a single row with daily_cap_removed_ok = true.
-- COALESCE so an empty pool (bool_and over zero rows is NULL) still reads ok.
SELECT COALESCE(bool_and(daily_cap = 0), true) AS daily_cap_removed_ok
  FROM mailjet_senders;
