# Outreach Automation — working notes

Python/FastAPI outreach pipeline: ingest contacts → find LinkedIn
profiles (Apollo) → scrape them (Apify) → AI-verify the match → draft
copy (LLM) → human review → send (Mailjet). ~800–900 contacts
per campaign; **correctness and message quality matter far more than
speed.** See `README.md` for the full architecture and failure
semantics — it is unusually detailed and is the source of truth.

## Layering — preserve it

- `repo.py` — ALL SQL. Nothing else touches the database.
- `providers/` — ALL vendor HTTP.
- `runner.py` / `scraper.py` / `drafting.py` / `verifier.py` /
  `emailer.py` — the loops. They never see a query string or an HTTP
  call, which is what makes them testable with fakes.
- `ui/routes.py` — server-rendered Jinja2 + vendored htmx, same process.

New vendor code takes a `transport: httpx.AsyncBaseTransport | None`
constructor argument so tests can inject `httpx.MockTransport`. Follow
`providers/mailjet.py`.

## Invariants

1. At most one first-touch email per contact, ever.
2. Approval is never automated — no path sends unreviewed copy.
3. Suppression is terminal and global, keyed on `lower(email)`.
4. `'sending'` is never auto-retried; stuck rows are surfaced, not reset.
5. Status means "where in the pipeline", never "what the outcome was".
6. A vendor failure releases claims — it is never a contact outcome.
7. Below `MIN_ACCEPT_CONFIDENCE`, discard the match.
8. Money is bounded (`MAX_PASSES`, `APIFY_MAX_ACTIVE_RUNS`, batch sizes).

## Tests

```
.venv/Scripts/python -m pytest                    # all, with coverage gate
.venv/Scripts/python -m pytest tests/test_x.py -q # one file
```

Fakes only — no database, no network, no real sleeping. `tests/conftest.py`
provides:

- **`_pinned_config`** (autouse) — pins every config value to its
  documented default. `config.py` calls `load_dotenv()` at import, so
  without this the suite reads whoever's `.env` is on disk. A test that
  needs a different value patches it itself; that always wins.
- **`_no_database`** (autouse) — a test that reaches a real `repo`
  function fails with a message saying so.
- **`patch_repo`** — swap repo functions for fakes by name.
- **`no_backoff`** — collapse retry sleeps, and assert the schedule.

Conventions: module docstring names the semantics the file locks in;
test names are claims about behaviour; comment the *why*, especially
where a test encodes a live incident.

Coverage has a floor (`--cov-fail-under` in `pytest.ini`). Raise it as
coverage rises; never lower it to make a red build green.

## QA

`/qa [module|diff|all]` runs the `qa-engineer` subagent — audits for
defects, writes regression tests, reviews diffs. Findings require a
file:line, a concrete failure path, and the invariant threatened.

## Gotchas

- `DATABASE_URL` must be the Supabase **session pooler** (port 5432,
  not 6543) — the claim queries hold locks across statements.
- Migrations are hand-applied in order in the Supabase SQL editor; each
  ends with a verification `SELECT` that must return `..._ok = true`.
  The base migration (001) is **not in this repo** — 006 backfills what
  the live schema was missing. Schema-dependent reasoning cannot be
  fully settled from source alone.
- An inactive n8n workflow 404s silently. Same trap for the ingest
  webhook, the LLM webhook, and the suppression webhook.
- **Never deploy to, or depend on, the n8n VPS** — it runs other
  production automations.
