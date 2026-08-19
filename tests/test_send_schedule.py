"""Batch-forecast projection for the Schedule page (app/send_schedule.py).

Pure functions, clock injected. Dates used: 2026-08-19 is a Wednesday,
08-21 Friday, 08-22 Saturday, 08-24 Monday. Window pinned to 9-5 weekdays,
5-minute interval, matching the drip's defaults.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app import send_schedule

ET = ZoneInfo("America/New_York")
WIN = dict(start_hour=9, end_hour=17, weekdays_only=True, interval_min=5)


def _senders(*emails):
    return [{"id": i + 1, "sender_email": e, "sender_name": e[0].upper()}
            for i, e in enumerate(emails)]


def _queue(n, **over):
    return [{"id": i, "email": f"c{i}@x.com", "first_name": f"C{i}",
             "last_name": "L", "company": "Co", "campaign_id": 1,
             "campaign_name": "Warmup", "pinned_sender_id": None, **over}
            for i in range(1, n + 1)]


# ---- batch_times ------------------------------------------------------


def test_times_step_by_the_interval_inside_the_window():
    ts = send_schedule.batch_times(3, datetime(2026, 8, 19, 10, 0, tzinfo=ET), **WIN)
    assert [t.strftime("%H:%M") for t in ts] == ["10:00", "10:05", "10:10"]


def test_times_roll_over_the_end_of_day():
    ts = send_schedule.batch_times(2, datetime(2026, 8, 19, 16, 57, tzinfo=ET), **WIN)
    assert ts[0].strftime("%a %H:%M") == "Wed 16:57"
    assert ts[1].strftime("%a %H:%M") == "Thu 09:00"     # 17:02 is out -> next open


def test_times_skip_the_weekend():
    ts = send_schedule.batch_times(2, datetime(2026, 8, 21, 16, 57, tzinfo=ET), **WIN)
    assert ts[1].strftime("%a %H:%M") == "Mon 09:00"     # Fri -> Mon, not Sat


def test_times_before_the_window_start_at_open():
    ts = send_schedule.batch_times(1, datetime(2026, 8, 19, 7, 0, tzinfo=ET), **WIN)
    assert ts[0].strftime("%a %H:%M") == "Wed 09:00"


def test_times_on_a_weekend_start_monday():
    ts = send_schedule.batch_times(1, datetime(2026, 8, 22, 12, 0, tzinfo=ET), **WIN)
    assert ts[0].strftime("%a %H:%M") == "Mon 09:00"


# ---- plan_batches -----------------------------------------------------


def test_queue_is_chunked_into_batches_of_the_batch_size():
    batches = send_schedule.plan_batches(
        _queue(5), _senders("a@x.com", "b@x.com"), batch_size=2,
        now=datetime(2026, 8, 19, 10, 0, tzinfo=ET), drip_active=True, **WIN)
    assert [len(b["contacts"]) for b in batches] == [2, 2, 1]
    assert [b["index"] for b in batches] == [1, 2, 3]


def test_each_batch_uses_each_mailbox_once_in_rotation():
    batches = send_schedule.plan_batches(
        _queue(4), _senders("a@x.com", "b@x.com"), batch_size=2,
        now=datetime(2026, 8, 19, 10, 0, tzinfo=ET), drip_active=True, **WIN)
    assert [c["mailbox"] for c in batches[0]["contacts"]] == ["a@x.com", "b@x.com"]
    assert [c["mailbox"] for c in batches[1]["contacts"]] == ["a@x.com", "b@x.com"]


def test_a_pinned_contact_draws_its_pinned_mailbox():
    senders = _senders("a@x.com", "b@x.com")   # ids 1, 2
    queue = _queue(2)
    queue[0]["pinned_sender_id"] = 2            # pinned to b@x.com
    batches = send_schedule.plan_batches(
        queue, senders, batch_size=2,
        now=datetime(2026, 8, 19, 10, 0, tzinfo=ET), drip_active=True, **WIN)
    mailboxes = [c["mailbox"] for c in batches[0]["contacts"]]
    assert mailboxes[0] == "b@x.com"            # the pin, not the rotation
    assert mailboxes[1] == "a@x.com"            # the rotating one still starts at slot 0


def test_times_are_none_when_the_drip_is_inactive():
    batches = send_schedule.plan_batches(
        _queue(3), _senders("a@x.com"), batch_size=1,
        now=datetime(2026, 8, 19, 10, 0, tzinfo=ET), drip_active=False, **WIN)
    assert all(b["at"] is None for b in batches)


def test_no_batches_without_senders_or_queue():
    now = datetime(2026, 8, 19, 10, 0, tzinfo=ET)
    assert send_schedule.plan_batches(_queue(3), [], batch_size=0, now=now,
                                      drip_active=True, **WIN) == []
    assert send_schedule.plan_batches([], _senders("a@x.com"), batch_size=1,
                                      now=now, drip_active=True, **WIN) == []
