-- 014: per-sender signature block, appended to the body at send time.
--
-- Every cold email must end with a fixed signature tied to the SENDING
-- address (the From, chosen at send time as the pool rotates). The AI now
-- writes no closing, and emailer._send_batch appends the picked sender's
-- signature. This column holds that block, edited per address on the Senders
-- page. NULL = no signature appended (surfaced in the UI so it isn't
-- forgotten).
--
-- Idempotent: ADD COLUMN IF NOT EXISTS. The pre-seed fills the six approved
-- addresses (two Madhav, four Aayush) and only when their signature is still
-- NULL, so it never clobbers an operator's later edit and is safe to re-run.
-- Rows that don't exist yet (address not synced) simply aren't touched.

ALTER TABLE mailjet_senders
    ADD COLUMN IF NOT EXISTS signature text;

-- Madhav Gupta — his own named addresses.
UPDATE mailjet_senders
   SET signature = 'Best,
Madhav Gupta
Founder’s Office (M&A)
Renegade Insurance
+1 678 500 9991'
 WHERE lower(sender_email) IN (
       'madhav.gupta@renegadeinsurance.info',
       'madhav.gupta@renegade-insurance.com'
   )
   AND signature IS NULL;

-- Aayush Gupta — his named addresses AND the shared business@ ones.
UPDATE mailjet_senders
   SET signature = 'Best,
Aayush Gupta
Founder’s Office (M&A)
Renegade Insurance
+1 678 500 9991'
 WHERE lower(sender_email) IN (
       'aayush.gupta@renegadeinsurance.info',
       'aayush.gupta@renegade-insurance.com',
       'business@renegadeinsurance.info',
       'business@renegade-insurance.com'
   )
   AND signature IS NULL;

-- Verification: must return a single row with signature_ok = true.
SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_name = 'mailjet_senders' AND column_name = 'signature'
) AS signature_ok;
