"""CSV parsing for the contact-upload form — pure logic, no I/O."""
from __future__ import annotations

import pytest

from app.config import config
from app.ui.csv_ingest import parse_contacts_csv


def test_header_synonyms_and_excel_bom():
    data = (
        "﻿Email,First Name,Last Name,Agency,Job Title\r\n"
        "a@b.c,Jane,Doe,Doe Insurance,Owner\r\n"
    ).encode("utf-8")

    rows, problems = parse_contacts_csv(data)

    assert rows == [{
        "email": "a@b.c",
        "first_name": "Jane",
        "last_name": "Doe",
        "company": "Doe Insurance",
        "title": "Owner",
    }]
    assert problems == []


def test_blank_rows_skipped_silently():
    data = b"email,first_name\na@b.c,Jane\n,\n\nb@c.d,Bob\n"
    rows, problems = parse_contacts_csv(data)
    assert [r["email"] for r in rows] == ["a@b.c", "b@c.d"]
    assert problems == []


def test_invalid_email_reported_and_skipped():
    data = b"email,first_name\nnot-an-email,Jane\nb@c.d,Bob\n"
    rows, problems = parse_contacts_csv(data)
    assert [r["email"] for r in rows] == ["b@c.d"]
    assert len(problems) == 1
    assert "line 2" in problems[0]


def test_unknown_columns_ignored():
    data = b"email,favourite_colour\na@b.c,teal\n"
    rows, _ = parse_contacts_csv(data)
    assert rows == [{"email": "a@b.c"}]


def test_missing_email_column_raises():
    with pytest.raises(ValueError, match="no email column"):
        parse_contacts_csv(b"first_name,company\nJane,Doe Insurance\n")


def test_size_cap_enforced(monkeypatch):
    monkeypatch.setattr(config, "CSV_MAX_BYTES", 10)
    with pytest.raises(ValueError, match="limit"):
        parse_contacts_csv(b"email\n" + b"a@b.c\n" * 10)


# ---- keep_extras: the CSV-only direct-insert path ---------------------


def test_keep_extras_captures_unmapped_columns():
    """CSV-only mode keeps every non-standard column under 'extra' so the
    drafter can personalize from it, and still maps the standard synonyms."""
    data = (
        "Email,First Name,State,Website,Notes\r\n"
        "a@b.c,Jane,TX,acme.example,Repeat buyer\r\n"
    ).encode("utf-8")

    rows, problems = parse_contacts_csv(data, keep_extras=True)

    assert problems == []
    assert rows[0]["email"] == "a@b.c"
    assert rows[0]["first_name"] == "Jane"
    # every standard field present (empty when the sheet lacks it) so the
    # direct INSERT never binds NULL to a possibly-NOT NULL base column
    assert rows[0]["last_name"] == "" and rows[0]["company"] == ""
    assert rows[0]["extra"] == {"State": "TX", "Website": "acme.example",
                                "Notes": "Repeat buyer"}


def test_keep_extras_drops_blank_extra_cells():
    data = b"email,state,notes\na@b.c,,kept\n"
    rows, _ = parse_contacts_csv(data, keep_extras=True)
    assert rows[0]["extra"] == {"notes": "kept"}   # empty 'state' dropped


def test_default_parse_still_drops_extras():
    """The n8n path is unchanged — extras are dropped and there's no 'extra'
    key (keep_extras defaults False)."""
    rows, _ = parse_contacts_csv(b"email,favourite_colour\na@b.c,teal\n")
    assert rows == [{"email": "a@b.c"}]


def test_csv_only_caps_are_independent(monkeypatch):
    """The CSV-only path passes its own, larger caps; the n8n default is
    untouched."""
    monkeypatch.setattr(config, "CSV_MAX_BYTES", 10)      # n8n default, tiny
    big = b"email\n" + b"a@b.c\n" * 50
    # keep_extras path with a generous max_bytes accepts it...
    rows, _ = parse_contacts_csv(big, keep_extras=True, max_bytes=10_000_000)
    assert rows
    # ...and still enforces the max_rows it is given.
    _, problems = parse_contacts_csv(big, keep_extras=True,
                                     max_bytes=10_000_000, max_rows=5)
    assert any("stopped at 5 rows" in p for p in problems)
