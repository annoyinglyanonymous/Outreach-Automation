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
  CSV). The objective is expanded by one n8n LLM call into the full brief
  (offer, CTA, tone, fallback template — degrades to a generic template
  if the LLM is unavailable). Sending needs no per-campaign setup: the
  From comes from the global sender pool (the **Senders** page), so a
  campaign is sendable as soon as the pool has an active domain. The edit
  page keeps every field, and its **Sending mailbox** dropdown optionally
  pins a campaign to one mailbox — every send goes only from that sender
  instead of rotating the pool (migration 011). It also has a **Send approved
  now** button that drains the campaign's approved queue immediately, bypassing
  the drip window/pacing. CSV upload proxies to the n8n ingest webhook
  (`N8N_INGEST_URL`).
- **Senders** — the global Mailjet From-rotation pool: add/edit/pause/delete
  validated sending addresses (an optional per-mailbox daily cap, off by
  default — see the drip below). Cold sends rotate least-recently-used across
  the active pool, one per mailbox per drip batch. The add form can pull the
  account's Mailjet-verified addresses (`GET /v3/REST/sender`), degrading to
  manual entry if Mailjet is unreachable.

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
prospect. Each email tick sends just **one drip batch** inside the send
window (see [Email sending](#email-sending-mailjet)), so the 5-minute
interval *is* the send cadence — it never drains the whole approved queue at
once. Set `SCHEDULER_STAGES` to a subset (e.g. drop `email`) to automate
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

Three paths, decided per contact:

- **`profile_data` present** → one LLM call producing a personalised
  email plus a LinkedIn connection note, returned as JSON. The ≤300-char
  note CHECK is enforced in code (one corrective retry, then a
  word-boundary clamp) because output schemas cannot express length.
- **Campaign in `enrichment_mode = 'csv'`** (migration 012) → one LLM call
  personalising from the contact's **captured sheet columns** (`extra_data`)
  instead of a scraped profile, via `drafting.build_csv_prompts` (same
  brief-based system prompt). These campaigns skip Apollo/Apify/verify
  entirely — see "CSV-only mode" below. Email only, so no LinkedIn note.
- **No profile, LinkedIn mode** → the campaign's `fallback_email_*` template
  rendered with `{{first_name}}`-style merge fields. No LLM call — the output
  is identical for everyone, so generating it N times costs money and adds
  variance for no benefit. No LinkedIn note: there is no profile to
  connect to.

### CSV-only mode (skip LinkedIn, personalise from the sheet)

A campaign's `enrichment_mode` (migration 012) is `'linkedin'` (default — the
full find→scrape→verify→draft pipeline) or `'csv'`. In `'csv'` mode — meant
for large lists (~12k) where per-contact LinkedIn enrichment isn't worth it —
the UI ingests the sheet **directly** (`repo.insert_csv_contacts`, the only
in-repo `INSERT INTO contacts`), bypassing the n8n webhook, capturing **every**
sheet column into `contacts.extra_data`, and landing each contact at
`linkedin_status = 'ready_to_draft'`. Because the enrich/scrape/verify claims
key off `'pending'`/`'enriched'`/a non-null `linkedin_url`, those contacts are
invisible to them and flow straight to drafting. Dedupe (in-batch +
per-campaign) and global suppression are enforced in the insert SQL (replacing
what n8n does for the LinkedIn path), so "at most one first-touch per contact"
and terminal suppression both still hold. Larger caps apply
(`CSV_ONLY_MAX_ROWS`/`CSV_ONLY_MAX_BYTES`) and the insert is chunked
(`CSV_INSERT_CHUNK`).

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
- `anthropic` — official SDK with structured outputs. Default model
  `claude-opus-4-8`; roughly $17 per 900-contact campaign. Note it has no
  `complete_json`, so it cannot power AI verification or brief expansion —
  those degrade to their manual/fallback paths under `anthropic`.

`DRAFT_MODEL` overrides the per-provider default. A refusal releases
just that contact and the run continues; rate-limit/5xx exhaustion
releases the unprocessed remainder and aborts, keeping drafts already
paid for.

## Email sending (Mailjet)

Requires migration 005. The gate is `email_status = 'drafted' AND
review_status = 'approved'`, and only this stage moves `email_status`
forward — which is what makes the business rule hold: **at most one
first-touch email per contact, ever**. No review/redraft sequence can
return a sent contact to the queue.

**Mailjet is the only sender.** Every approved draft is sent via the Send
API v3.1 (`POST /v3.1/send`, HTTP Basic auth over
`MAILJET_API_KEY`/`MAILJET_SECRET_KEY`), with the drafted subject/body
posted as the message. The body is sent as **two parts**: the stored plain
text as the text/plain alternative, and an HTML rendering of the same copy
(`email_format.render_html_body`) as the text/html part, so it lands as a
formatted email — paragraphs, line breaks, clickable links — instead of an
unstyled blob. The stored copy stays plain text (one editable source of
truth; the review UI is unchanged) and the HTML is derived at send time.
`consent_status` is a record-only field now; it no longer picks a vendor.

The **From is rotated** across a global sender pool (`mailjet_senders`,
migration 010): each send draws the least-recently-used active domain, so no
single domain carries all the volume — the one deliverability lever available
on an ESP. Manage the pool on the **Senders** page. ⚠️ Mailjet is a bulk ESP
whose AUP forbids cold outreach and whose shared IPs hurt cold deliverability
— a deliberate, owner-directed choice, not a recommended default.

**Sending is a business-hours drip, not a burst** (the pacing model). One
`emailer.run()` sends exactly **one batch — one email per active mailbox**
(sized to `count_active_senders`, bounded by `SEND_BATCH_SIZE`) and stops; the
scheduler tick (`SCHEDULER_INTERVAL_MINUTES`, default 5) is the cadence, so an
approved list drains a batch at a time (~`active × 12 × 8`/day). Sends only
inside the send window — `SEND_WINDOW_START_HOUR`..`SEND_WINDOW_END_HOUR` in
`SEND_WINDOW_TZ` (default 9–5 `America/New_York`), weekdays unless
`SEND_WINDOW_WEEKDAYS_ONLY=false`; outside it the run is a clean no-op
(`emailer.within_send_window`, DST-correct via `zoneinfo` — hence the `tzdata`
dependency). The **per-sender daily cap is off by default**: `daily_cap <= 0`
means "no cap" in every claim, because the drip (batch size × tick × window)
is now the throttle. Set a positive `daily_cap` on the Senders page to throttle
one warming mailbox; migration 015 zeroes the column for the drip model.

A campaign may instead be **pinned to one mailbox** (`campaigns.pinned_sender_id`,
migration 011) — e.g. a named-person sequence where every touch must come
from the same address. A pinned campaign draws that one sender only, via
`claim_pinned_sender`; the pin narrows *which* sender is used. The claim's
capacity gate is per-campaign: a pinned mailbox that is paused/deleted (or, if
you set one, at its `daily_cap`) stops *that* campaign without pausing others,
and a pin to a paused/deleted sender is surfaced as "approved but unsendable",
never silently rotated back to the pool (only a hard-deleted sender reverts a
campaign to rotation).

**Send approved now** (`POST /ui/campaigns/{id}/send-now`,
`emailer.send_campaign_now`) is a manual override on the campaign page: it
drains *that one campaign's* approved queue immediately, bypassing the window
and the 5-minute pacing — for warming a mailbox or a small urgent batch. Every
other guarantee still holds (suppression sweep, one-first-touch claim,
allowlist/rotation, appended signature, per-send write, money-grade release);
it is bounded by `MAX_PASSES` per click. Pin the campaign to send all from one
mailbox.

Mailjet has **no idempotency key**, so an ambiguous failure *after* the
request left us raises `SendUncertain`: the contact is left at `'sending'`
(surfaced as stuck, human-resolved) rather than released, because a replay
could double-send. A hard rejection returns the rotating sender's daily
slot; an uncertain send keeps it (the mail may have counted).

Safety semantics worth knowing:

- Suppression is swept immediately before every run (and re-checked in
  the claim); suppressed contacts are terminal, approval notwithstanding.
- Contacts stuck at `'sending'` (crash between vendor accept and our
  write, or a Mailjet `SendUncertain`) are **never auto-retried** — the
  dashboard and `/stats` surface them; resolve against the Mailjet
  dashboard by hand.
- `'failed'` (hard rejection) is terminal; re-sending is a human decision.
- Campaigns approved while the sender pool has no active domain are never
  claimed and cannot starve other campaigns; the dashboard lists them as
  "approved but unsendable" with the reason.

### Suppression, unsubscribes & delivery outcomes

The `suppression` table (migration 006) is the global do-not-contact list,
keyed on `lower(email)`. Every email run sweeps it first (drafted contacts
whose email is listed flip to `suppressed`, terminal) and the send claim
re-checks it, so a suppressed address is never mailed by any campaign or
either vendor. The app only ever *reads* this table.

Live outcomes from Mailjet sends are fed back by an n8n webhook
(`docs/n8n-mailjet-events.json`), not app code — the app is not publicly
reachable, n8n already is and already writes to the same Supabase. Mailjet's
Event API POSTs each event to the workflow (batched as an array when event
grouping is on — the classifier handles both), which applies at most two
effects, each guarded independently:

- `unsub` / `spam` / `blocked` → add to `suppression` (do-not-contact).
- hard `bounce` (`hard_bounce: true`) → add to `suppression` **and** set the
  contact `email_status = 'bounced'` (soft bounces are transient, ignored).
- `sent` → resolve a contact stuck at `sending` (the crash window between
  vendor-accept and our write) to `sent_email`.

**Reply detection is gone with Smartlead:** Mailjet is send-only and never
emits a reply event, so there is no `email_status = 'replied'` path and the
reply-rate KPI has no source here — restoring it needs Mailjet Inbound Parse
or a separate reply-inbox integration.

The status write **only ever advances** a contact (`sending → sent_email /
bounced`, `sent_email → bounced`) — never back to a re-sendable state — so
the at-most-one-send invariant holds, and stuck `sending` is resolved by
Mailjet's own confirmation rather than a guessed retry. Contacts are matched
by the **`CustomID`** we stamp on every send (`outreach-contact-<id>`); the
cast is `NULLIF($4,'')::bigint` so an event without a CustomID (one for a
message this app didn't send) resolves to a harmless no-op instead of a
plan-time cast error. Suppression is keyed on `lower(email)` and its insert
is idempotent (`WHERE NOT EXISTS`); an event is logged (`suppression_added` /
`spam_suppressed` / `blocked_suppressed` / `bounce_suppressed` /
`send_confirmed`) only when something actually changed, doubling as a
dashboard heartbeat — none end in `_failed`, so the error-rate KPI is
untouched. No new migration: `bounced` / `sent_email` are already in the
`email_status` CHECK (migration 007).

Setup: import `docs/n8n-mailjet-events.json`, select the Supabase Postgres
credential in the "Apply to DB" node, **activate it** (an inactive workflow
silently stops recording unsubscribes — a compliance risk, hence the
heartbeat events), then add an Event API webhook in Mailjet (Account →
Event tracking / triggers) pointed at the workflow URL, subscribed to
sent, bounce, blocked, spam and unsub. (`docs/n8n-suppression-webhook.json`
is the retired Smartlead-era version, kept for reference only.)

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
- Email sends through **Mailjet only** (owner-directed). Bulk ESPs
  (Mailjet/Resend/Postmark) prohibit cold outreach under their AUP; the
  From is rotated across many validated domains to spread reputation, the
  one lever available on an ESP. `consent_status` is a record-only field.
- LinkedIn sending (future stage) drives real accounts via Unipile;
  `accounts.daily_limit` (default 15) is deliberately conservative.
