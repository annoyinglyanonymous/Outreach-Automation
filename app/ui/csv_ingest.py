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

# The canonical fields the ingest webhook expects. In keep_extras mode every
# one is emitted (empty when the sheet lacks it) so the direct INSERT's
# json_to_recordset never binds NULL to a possibly-NOT-NULL base column.
STANDARD_FIELDS = ("email", "first_name", "last_name", "company", "title")


def parse_contacts_csv(
    data: bytes,
    *,
    keep_extras: bool = False,
    max_bytes: int | None = None,
    max_rows: int | None = None,
) -> tuple[list[dict], list[str]]:
    """Returns (rows, problems). Rows are ready for the ingest webhook;
    problems are human-readable and non-fatal unless rows is empty.

    Default (keep_extras=False): the n8n path — each row carries only the
    mapped standard fields, exactly as before. With keep_extras=True (the
    CSV-only direct-insert path): every standard field is present (empty when
    absent) and every OTHER sheet column rides along under ``row['extra']``,
    keyed by original header, so the drafter can personalize from it. The
    caps default to the n8n path's; the CSV-only path passes its larger ones.
    """
    max_bytes = config.CSV_MAX_BYTES if max_bytes is None else max_bytes
    max_rows = MAX_ROWS if max_rows is None else max_rows
    if len(data) > max_bytes:
        raise ValueError(f"file is {len(data)} bytes; the limit is {max_bytes}")

    # utf-8-sig: Excel exports open with a BOM that would otherwise glue
    # itself onto the first header name.
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("no header row found")

    mapping: dict[str, str] = {}
    extra_headers: list[str] = []
    for raw in reader.fieldnames:
        key = (raw or "").strip().lower()
        if key in HEADER_MAP:
            mapping[raw] = HEADER_MAP[key]
        elif keep_extras and raw and raw.strip():
            extra_headers.append(raw)

    if "email" not in mapping.values():
        raise ValueError(
            f"no email column found — headers were: {', '.join(reader.fieldnames)}"
        )

    rows: list[dict] = []
    problems: list[str] = []
    for line_no, raw_row in enumerate(reader, start=2):
        if len(rows) >= max_rows:
            problems.append(f"stopped at {max_rows} rows; remainder ignored")
            break
        row = {field: (raw_row.get(header) or "").strip()
               for header, field in mapping.items()}
        if not any(row.values()):
            continue  # blank line
        if "@" not in row.get("email", ""):
            problems.append(f"line {line_no}: missing or invalid email, skipped")
            continue
        if keep_extras:
            for field in STANDARD_FIELDS:
                row.setdefault(field, "")
            extra = {h.strip(): (raw_row.get(h) or "").strip() for h in extra_headers}
            row["extra"] = {k: v for k, v in extra.items() if k and v}
        rows.append(row)

    return rows, problems
