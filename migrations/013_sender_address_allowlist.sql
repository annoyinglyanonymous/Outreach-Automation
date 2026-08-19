-- 013: enforce the cold-send address allowlist on the existing pool.
--
-- Cold sends may go out only from an approved sending address (see
-- config.SENDER_ALLOWED_ADDRESSES). Ongoing enforcement lives in the app: the
-- sync filters Mailjet's verified list to these addresses, the Senders form
-- rejects others, and the toggle refuses to resume a non-approved sender.
-- Because every send path already requires WHERE active, keeping non-approved
-- senders paused (active = false) is the whole guardrail — no claim changes.
--
-- This migration is the one-time cleanup of rows auto-enrolled (and left
-- active) BEFORE the policy existed, so the live pool is correct immediately
-- rather than waiting for the next sync to pause them. The address list is
-- hardcoded here as a point-in-time cleanup; the live allowlist is
-- config-driven and may change there without another migration.
--
-- Idempotent: re-running only re-pauses anything that drifted back active.

UPDATE mailjet_senders
   SET active = false
 WHERE active
   AND lower(sender_email) NOT IN (
       'madhav.gupta@renegadeinsurance.info',
       'madhav.gupta@renegade-insurance.com',
       'business@renegadeinsurance.info',
       'business@renegade-insurance.com',
       'aayush.gupta@renegadeinsurance.info',
       'aayush.gupta@renegade-insurance.com'
   );

-- Verification: must return a single row with off_list_active_ok = true
-- (no active sender remains outside the allowlist).
SELECT NOT EXISTS (
    SELECT 1 FROM mailjet_senders
     WHERE active
       AND lower(sender_email) NOT IN (
           'madhav.gupta@renegadeinsurance.info',
           'madhav.gupta@renegade-insurance.com',
           'business@renegadeinsurance.info',
           'business@renegade-insurance.com',
           'aayush.gupta@renegadeinsurance.info',
           'aayush.gupta@renegade-insurance.com'
       )
) AS off_list_active_ok;
