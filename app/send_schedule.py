"""Read-only projection of the business-hours drip for the Schedule page:
group the approved-unsent queue into the batches the email stage will send, and
estimate when each goes out.

Pure functions with the clock injected, so the page is testable and a test
never depends on the wall clock. The projection is an ESTIMATE — it assumes the
scheduler keeps firing every interval inside the window with the current
active-sender set and no failures; real timing shifts with pauses, vendor
errors, and cap/allowlist changes. See app/emailer.run (the drip) and
app/repo.approved_unsent_queue (the claim order this mirrors).
"""
from __future__ import annotations

from datetime import datetime, timedelta


def _in_window(dt: datetime, start_hour: int, end_hour: int,
               weekdays_only: bool) -> bool:
    if weekdays_only and dt.weekday() >= 5:
        return False
    return start_hour <= dt.hour < end_hour


def _advance_to_open(dt: datetime, start_hour: int, end_hour: int,
                     weekdays_only: bool) -> datetime:
    """Move `dt` forward to the next moment the send window is open. Bounded so
    a misconfiguration (e.g. start_hour >= end_hour) can't loop forever."""
    for _ in range(21):  # ~3 weeks of daily hops — far more than ever needed
        if _in_window(dt, start_hour, end_hour, weekdays_only):
            return dt
        if weekdays_only and dt.weekday() >= 5:
            dt = (dt + timedelta(days=1)).replace(
                hour=start_hour, minute=0, second=0, microsecond=0)
        elif dt.hour < start_hour:
            dt = dt.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        else:  # at/after end_hour
            dt = (dt + timedelta(days=1)).replace(
                hour=start_hour, minute=0, second=0, microsecond=0)
    return dt


def batch_times(count: int, now: datetime, *, start_hour: int, end_hour: int,
                weekdays_only: bool, interval_min: int) -> list[datetime]:
    """Projected send time of each of the next `count` batches, spaced by
    `interval_min` inside the window and rolling over nights/weekends."""
    times: list[datetime] = []
    t = _advance_to_open(now, start_hour, end_hour, weekdays_only)
    for _ in range(count):
        times.append(t)
        t = _advance_to_open(t + timedelta(minutes=interval_min),
                             start_hour, end_hour, weekdays_only)
    return times


def plan_batches(queue: list[dict], senders: list[dict], *, batch_size: int,
                 now: datetime, drip_active: bool, start_hour: int,
                 end_hour: int, weekdays_only: bool,
                 interval_min: int) -> list[dict]:
    """Group `queue` (approved-unsent, in claim order) into batches of
    `batch_size`, assign each contact a projected From mailbox, and — when
    `drip_active` — a projected send time (else ``at`` is None: sending is
    manual/paused).

    Returns ``[{index, at, contacts: [{...contact, mailbox}]}]``. Mailbox
    projection mirrors the pick: a pinned campaign's contact draws its pinned
    mailbox; every other contact draws the next active sender in rotation (each
    mailbox once per batch)."""
    if batch_size <= 0 or not queue:
        return []
    by_id = {s["id"]: s["sender_email"] for s in senders}
    rotation = [s["sender_email"] for s in senders]
    chunks = [queue[i:i + batch_size] for i in range(0, len(queue), batch_size)]
    times = (batch_times(len(chunks), now, start_hour=start_hour,
                         end_hour=end_hour, weekdays_only=weekdays_only,
                         interval_min=interval_min)
             if drip_active else [None] * len(chunks))
    batches = []
    for bi, chunk in enumerate(chunks):
        rot = 0
        contacts = []
        for c in chunk:
            pin = c.get("pinned_sender_id")
            if pin and pin in by_id:
                mailbox = by_id[pin]
            elif rotation:
                mailbox = rotation[rot % len(rotation)]
                rot += 1
            else:
                mailbox = None
            contacts.append({**c, "mailbox": mailbox})
        batches.append({"index": bi + 1, "at": times[bi], "contacts": contacts})
    return batches
