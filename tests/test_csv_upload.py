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
