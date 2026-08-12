# Outreach Automation — Enrichment & Scraping Service

Python/FastAPI service that takes ingested contacts (insurance agents,
US market), finds their LinkedIn profiles (Apollo), and scrapes profile
content (Apify) so a later drafting stage can personalise messages.
Roughly 800–900 contacts per campaign; correctness and message quality
matter far more than speed.

## Architecture

Queue-driven, not pipeline-chained: every stage claims work from
Postgres by status (`FOR UPDATE SKIP LOCKED`) and writes results back.
No stage calls the next, so a broken stage cannot cascade — contacts
pile up harmlessly at the previous status and resume when it is fixed.

```
pending → enriching → enriched → scraping → ready_to_draft → drafted
                    ↘ (no URL found) ─────→ ready_to_draft ↗
```

The status column means only "where in the pipeline", never "what the
outcome was". A contact whose LinkedIn was not found still reaches
`ready_to_draft`; the failure lives in `events` and is derivable from
`linkedin_url IS NULL` (likewise `profile_data IS NULL` for scraping).

Layering, please preserve:

- `repo.py` — ALL SQL. Nothing else touches the database.
- `providers/` — ALL vendor HTTP (Apollo, Apify).
- `runner.py` / `scraper.py` — the loops. They never see a query string
  or an HTTP call, which is what makes them testable with fakes.

## Setup

```
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt -r requirements-dev.txt
copy .env.example .env    # then fill in credentials
.venv/Scripts/python -m uvicorn app.api:app
```

`DATABASE_URL` must use the Supabase **session pooler** (port 5432, not
the transaction pooler on 6543) — the claim queries hold locks across
statements. Do not keep the `[]` from Supabase's password placeholder.

Migrations are hand-applied: run each `migrations/*.sql` in order in the
Supabase SQL editor; every file is idempotent and ends with a
verification SELECT that must return `..._ok = true`. Note 006: the
original base migration (001) never made it into this repo, and the live
schema was missing the `events` table entirely — 006 backfills it (plus
`suppression` and `contacts.linkedin_confidence` guards). Without it,
every stage that logs an event fails, as does /ui/verify and the
dashboard event feed.

## Web UI

Server-rendered pages (Jinja2 + vendored htmx) inside the same service at
`/ui` — no separate deploy. Login checks credentials against Supabase
Auth (invite teammates via the Supabase dashboard); the app then issues
its own signed session cookie, so no JWT machinery exists here. Pages:

- **Dashboard** — per-stage counts (10s poll), stage running/idle
  statusline, event feed. (The pipeline runs itself; there are no manual
  Run buttons.)
- **Verify** — the AI `verify` stage judges each match automatically
  (right → keep the personalized draft; wrong/unsure → email-only), so
  this page is the **audit trail + manual fallback**: recent verdicts with
  reason/confidence/reviewer, an override to mark an AI-confirmed match
  wrong, and the still-unverified queue (lowest confidence first) for when
  the AI is off/down. "Wrong person" atomically clears the URL, profile and
  any draft and re-queues the contact email-only; verdicts land in `events`.
- **Review** — drafted email + LinkedIn note beside the scraped profile;
  edit inline, then Save / Save & approve / Save & reject (one action, so
  approving always persists the edits on screen) or Re-draft. Future send
  stages take only `review_status = 'approved'` contacts, and a re-draft
  resets the review — an approval can never outlive the copy it approved.
- **Campaigns** — quick create: name + objective + sender (+ optional
  CSV). The objective is expanded by one Groq call into the full brief
  (offer, CTA, tone, fallback template — degrades to a generic template
  if the LLM is unavailable), and for cold campaigns the Smartlead
  campaign is built automatically: shell sequence, every connected
  mailbox, a conservative schedule (`SMARTLEAD_SCHEDULE_*`), activated,
  id written back. Vendor failures never block creation — outcomes
  surface as flashes, and the edit page has a "Set up Smartlead" retry.
  The edit page keeps every field; CSV upload proxies to the n8n ingest
  webhook (`N8N_INGEST_URL`).

Requires migrations 003 + 004 and the `SUPABASE_URL` /
`SUPABASE_ANON_KEY` / `SESSION_SECRET` env vars (see .env.example).

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /ui/…` | browser session | web UI (see above) |
| `GET /` | — | endpoint listing |
| `GET /health` | — | liveness + database reachability |
| `GET /stats` | `x-api-key` | queue counts, runs in flight |
| `POST /enrich/run` | `x-api-key` | start an enrichment run (202, background) |
| `POST /scrape/run` | `x-api-key` | collect finished Apify runs, then start new ones (202, background) |
| `POST /draft/run` | `x-api-key` | draft emails + LinkedIn notes for `ready_to_draft` contacts (202, background) |

The run endpoints take **no payload** — each runner claims its own work
from the queue. Trigger them on a schedule; a duplicate trigger while a
run is active returns 409.

## Automation (scheduler)

The pipeline is manual by default — `POST` the run endpoints. To run it
unattended, set `SCHEDULER_ENABLED=true` and an in-process scheduler
(`app/scheduler.py`, APScheduler) triggers the stages every
`SCHEDULER_INTERVAL_MINUTES` (default 5).

It runs *inside* the FastAPI process on purpose — no external cron, and
nothing on the n8n VPS. Each tick calls the same guarded `runs.try_start`
the endpoints do, so a scheduled run never overlaps a prior tick; a stage
that is already running, or missing its config, is skipped and retried next
interval. Because every stage is idempotent and drains its own queue,
firing them all on one interval is safe — work a stage isn't ready for yet
is picked up on a later tick.

**Approval is never automated.** The scheduler drives enrich → scrape →
verify → draft → email, but the email stage only ever claims contacts a
human approved in the Review page, so no unreviewed copy can reach a
prospect. Set `SCHEDULER_STAGES` to a subset (e.g. drop `email`) to automate
the upstream stages while keeping sending manual. `/stats` reports whether
automation is on under `scheduler`.

On top of the interval, **completion nudges** make the flow tick-free:
a successful CSV ingest starts enrichment immediately, a finished enrich
run starts scraping, a finished scrape starts verification, a finished
verification starts drafting, and approving a draft starts the email run —
all through the same one-run-per-stage guard (`runs.nudge`), with the
scheduler as the safety net. The nudge walks *past* a stage that's
unconfigured or off (e.g. `verify` disabled → scrape nudges draft
directly). There is deliberately no draft→email nudge: only a human
approval opens that gate. Net effect: upload a CSV and drafts appear in
Review with zero clicks; approve, and the send leaves within seconds.

## Enrichment (Apollo)

Tier 0 is a free cache lookup against past campaigns (≤365 days old,
confidence ≥0.70). Tier 1 is Apollo bulk match; Apollo returns no
confidence score, so one is derived from agreement between what was sent
and what came back. Matches below `MIN_ACCEPT_CONFIDENCE` are discarded
(a URL we cannot attribute confidently is worse than none — the drafter
would personalise a message to a stranger).

Provider errors (429/5xx/timeout) release the claimed contacts back to
`pending` and abort the run: "the vendor was down" is not "this person
has no LinkedIn".

## Scraping (Apify)

Apify runs are asynchronous, so the stage is a starter + collector pair:
each `/scrape/run` first reconciles runs that finished since the last
trigger, then claims new batches (one actor run per batch) up to
`APIFY_MAX_ACTIVE_RUNS` concurrent runs. Nothing blocks; a scheduled
trigger drains the queue over successive invocations.

- Actor: `harvestapi/linkedin-profile-scraper`, **cookieless** — actors
  requiring a LinkedIn session cookie carry account-ban risk and are
  off-limits. Mode is pinned to "no email" ($4/1k); the source lists
  already carry emails.
- A failed/expired run releases its contacts back to `enriched` for
  re-scrape. A profile missing from a successful run's dataset is an
  outcome: the contact proceeds to `ready_to_draft` with
  `profile_data IS NULL` and drafting falls back to the template.
- If a run succeeds with a non-empty dataset but zero items match our
  URLs, the collector refuses to write (that is a field-mapping
  misconfiguration, not 50 vanished profiles) and leaves the run
  claimed — the dataset persists, so re-collecting after fixing
  `APIFY_URL_FIELDS` costs nothing.

## Verification (AI)

Between scrape and draft, the `verify` stage judges whether the LinkedIn
profile a vendor matched actually belongs to the contact — a wrong match
would personalize a cold email to a stranger. For each **profile-bearing**
contact with no verdict yet, one LLM call (the same route as drafting,
`complete_json` — no new n8n workflow) returns `{verdict, confidence,
reason}`; the runner applies it through the *same* functions the manual
verify page uses: `right_person` → confirm (keep the personalized path),
`wrong_person`/`unsure` → reject (wipe the match; the contact drops to the
template/email-only draft). Conservative by contract — anything short of a
clear same-person match is `unsure`, i.e. rejected, because a wrong
personalized email is worse than a generic one.

No status flip and no migration: a verdict is an `events` row, and the
runner remembers the contacts it has attempted this run so the queue
drains and the loop terminates. It does **not** gate draft — the chain
(`scrape → verify → draft`) runs verify first, and if drafting ever races
ahead, a later reject clears the draft and re-queues email-only
(self-correcting). Off (`VERIFY_ENABLED=false`) or with no `complete_json`
provider configured, the stage no-ops, scrape nudges draft directly, and
the human `/ui/verify` page is the path. `VERIFY_BATCH_SIZE` bounds each
pass. Profile-less matches are template-drafted regardless, so they're left
for the queue rather than spending an LLM call.

## Drafting (LLM)

Two paths, decided per contact (requires migration 003):

- **`profile_data` present** → one LLM call producing a personalised
  email plus a LinkedIn connection note, returned as JSON. The ≤300-char
  note CHECK is enforced in code (one corrective retry, then a
  word-boundary clamp) because output schemas cannot express length.
- **No profile** → the campaign's `fallback_email_*` template rendered
  with `{{first_name}}`-style merge fields. No LLM call — the output is
  identical for everyone, so generating it N times costs money and adds
  variance for no benefit. No LinkedIn note: there is no profile to
  connect to.

The vendor is a provider behind one protocol — `DRAFT_PROVIDER` selects
it, nothing else changes:

- `n8n` — an n8n webhook fronting the org's OpenAI credential
  (gpt-4o-mini; ~$0.20 per 900-contact campaign). Setup: import
  `docs/n8n-llm-workflow.json` into n8n, open the "OpenAI Chat" node
  and select the org's OpenAI credential, **activate the workflow**
  (an inactive workflow 404s — same trap as the ingest webhook), then
  put the production webhook URL in `N8N_LLM_URL`. Model changes are
  an n8n node edit, not a deploy. The same webhook also powers the
  quick-create brief expansion.
- `groq` — OpenAI-compatible endpoint via httpx, JSON mode. Default
  model `openai/gpt-oss-120b`; well under $1 per 900-contact campaign.
  Free-tier warning: keep prompt+`DRAFT_MAX_TOKENS` inside the 8000
  tokens-per-request budget, but not so low that reasoning eats the
  output (both failure modes were hit live).
- `anthropic` — official SDK with structured outputs. Default model
  `claude-opus-4-8`; roughly $17 per 900-contact campaign.

`DRAFT_MODEL` overrides the per-provider default. A refusal releases
just that contact and the run continues; rate-limit/5xx exhaustion
releases the unprocessed remainder and aborts, keeping drafts already
paid for.

## Email sending (Smartlead / Resend)

Requires migration 005. The gate is `email_status = 'drafted' AND
review_status = 'approved'`, and only this stage moves `email_status`
forward — which is what makes the business rule hold: **at most one
first-touch email per contact, ever**. No review/redraft sequence can
return a sent contact to the queue.

Per campaign, `consent_status` picks the vendor (the AUP constraint):

- **`cold` → Smartlead.** Create a Smartlead campaign whose sequence is
  exactly the merge shell `{{personalized_subject}}` /
  `{{personalized_body}}`, attach warmed mailboxes, and put its id in
  the campaign form. Each approved contact is pushed as a lead carrying
  the drafted copy; Smartlead schedules delivery, applies its block and
  unsubscribe lists, and skips leads already in the campaign (which is
  what makes crash-recovery replays safe).
- **`opted_in` → Resend.** Needs a verified sending domain and the
  campaign's `sender_email`. Every send carries
  `Idempotency-Key: outreach/contact-{id}` so retries cannot double-send.

Safety semantics worth knowing:

- Suppression is swept immediately before every run (and re-checked in
  the claim); suppressed contacts are terminal, approval notwithstanding.
- Contacts stuck at `'sending'` (crash between vendor accept and our
  write) are **never auto-retried** — the dashboard and `/stats` surface
  them; resolve against the provider dashboard by hand.
- `'failed'` (hard rejection) is terminal; re-sending is a human decision.
- Misconfigured campaigns (missing Smartlead id / sender email / API key)
  are never claimed and cannot starve other campaigns; the dashboard
  lists them as "approved but unsendable" with the reason.

### Suppression, unsubscribes & delivery outcomes

The `suppression` table (migration 006) is the global do-not-contact list,
keyed on `lower(email)`. Every email run sweeps it first (drafted contacts
whose email is listed flip to `suppressed`, terminal) and the send claim
re-checks it, so a suppressed address is never mailed by any campaign or
either vendor. The app only ever *reads* this table.

Live outcomes from Smartlead sends are fed back by an n8n webhook
(`docs/n8n-suppression-webhook.json`), not app code — the app is not
publicly reachable, n8n already is and already writes to the same Supabase.
Smartlead POSTs every event to the workflow, which classifies it and applies
at most two effects, each guarded independently:

- `LEAD_UNSUBSCRIBED` → add to `suppression`.
- hard `EMAIL_BOUNCE` → add to `suppression` **and** set the contact
  `email_status = 'bounced'` (soft bounces are transient and ignored).
- `EMAIL_REPLY` → set `email_status = 'replied'` (a reply is not a
  do-not-contact, so no suppression).
- `EMAIL_SENT` / `FIRST_EMAIL_SENT` → resolve a contact stuck at `sending`
  (the crash window between vendor-accept and our write) to `sent_email`.

The status write **only ever advances** a contact (`sending → sent_email /
replied / bounced`, `sent_email → replied / bounced`) — never back to a
re-sendable state — so the at-most-one-send invariant holds, and stuck
`sending` is resolved by Smartlead's own confirmation rather than a guessed
retry. Contacts are matched by `lower(email)` **and** the campaign's
`smartlead_campaign_id`, so an event for a campaign this app doesn't own
(matched by neither) is a harmless no-op. The suppression insert is
idempotent (`WHERE NOT EXISTS` on `lower(email)`); an event is logged
(`suppression_added` / `reply_received` / `bounce_suppressed` /
`send_confirmed`) only when something actually changed, doubling as a
dashboard heartbeat — none end in `_failed`, so the error-rate KPI is
untouched. **Requires migration 007**, which adds `replied` / `bounced` to
the `email_status` CHECK; apply it *before* activating the workflow or those
writes violate the constraint and Smartlead retries the 500 forever.

Setup: apply migration 007, import the workflow, select the Supabase
Postgres credential in the "Apply to DB" node, **activate it** (an inactive
workflow silently stops recording unsubscribes — a compliance risk, hence
the heartbeat events), then register its URL in Smartlead's webhook settings
subscribed to the unsubscribe, bounce, reply and sent events. Still deferred
(so n8n's blast radius stays small): the Resend `List-Unsubscribe` header
and a Resend-side webhook, i.e. the opted-in path's own outcome feedback.

## Tests

```
.venv/Scripts/python -m pytest
```

Fakes only — no database or vendor calls. The suite locks in the
failure semantics above; enrichment runner/scoring tests are still to
be formalised from the original throwaway scripts.

## Deployment constraints

- **Never deploy to, or depend on, the n8n VPS.** It runs other
  production automations.
- Email sending (future stage) must branch on `consent_status`:
  Resend/Postmark prohibit cold outreach; cold lists go through
  cold-email infrastructure.
- LinkedIn sending (future stage) drives real accounts via Unipile;
  `accounts.daily_limit` (default 15) is deliberately conservative.
