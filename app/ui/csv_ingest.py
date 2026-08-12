"""CSV parsing for the contact-upload form. Pure logic, no I/O."""
from __future__ import annotations

import csv
import io

from ..config import config

# Header synonyms → canonical field names the ingest webhook expects.
HEADER_MAP = {
    "email": "email",
    "e-mail": "email",
    "email address": "email",
    "first name": "first_name",
    "first_name": "first_name",
    "firstname": "first_name",
    "last name": "last_name",
    "last_name": "last_name",
    "lastname": "last_name",
    "surname": "last_name",
    "company": "company",
    "company name": "company",
    "organization": "company",
    "organisation": "company",
    "agency": "company",
    "title": "title",
    "job title": "title",
    "job_title": "title",
    "role": "title",
}

MAX_ROWS = 2000


def parse_contacts_csv(data: bytes) -> tuple[list[dict], list[str]]:
    """Returns (rows, problems). Rows are ready for the ingest webhook;
    problems are human-readable and non-fatal unless rows is empty."""
    if len(data) > config.CSV_MAX_BYTES:
        raise ValueError(
            f"file is {len(data)} bytes; the limit is {config.CSV_MAX_BYTES}"
        )

    # utf-8-sig: Excel exports open with a BOM that would otherwise glue
    # itself onto the first header name.
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("no header row found")

    mapping: dict[str, str] = {}
    for raw in reader.fieldnames:
        key = (raw or "").strip().lower()
        if key in HEADER_MAP:
            mapping[raw] = HEADER_MAP[key]

    if "email" not in mapping.values():
        raise ValueError(
            f"no email column found — headers were: {', '.join(reader.fieldnames)}"
        )

    rows: list[dict] = []
    problems: list[str] = []
    for line_no, raw_row in enumerate(reader, start=2):
        if len(rows) >= MAX_ROWS:
            problems.append(f"stopped at {MAX_ROWS} rows; remainder ignored")
            break
        row = {field: (raw_row.get(header) or "").strip()
               for header, field in mapping.items()}
        if not any(row.values()):
            continue  # blank line
        if "@" not in row.get("email", ""):
            problems.append(f"line {line_no}: missing or invalid email, skipped")
            continue
        rows.append(row)

    return rows, problems
