"""All UI routes. Pages are full renders; fragments are htmx swap targets.

Every route except login sits behind require_session; every mutating
route also passes check_origin. No SQL and no vendor HTTP in this
module — repo.py and providers/ own those.
"""
from __future__ import annotations

import json
import logging

import asyncpg
from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
# Starlette's class, not fastapi.UploadFile: request.form() yields the
# base class, and isinstance against the fastapi subclass silently drops
# every upload.
from starlette.datastructures import UploadFile

from .. import campaign_brief, repo, runs
from ..config import config
from ..drafting import NOTE_MAX_CHARS
from ..providers import n8n, smartlead, supabase_auth
from ..providers.base import ProviderError
from . import router, templates
from .auth import (
    COOKIE_NAME,
    Session,
    check_origin,
    require_session,
    safe_next,
    sign_session,
)
from .csv_ingest import parse_contacts_csv

log = logging.getLogger(__name__)

PIPELINE_ORDER = (
    "pending", "enriching", "enriched", "scraping",
    "ready_to_draft", "drafting", "drafted",
)


def _page(request: Request, name: str, session: Session | None, active: str,
          extra: dict | None = None, status_code: int = 200):
    context = {"session": session, "active": active, **(extra or {})}
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def safe_linkedin_url(url: str | None) -> str | None:
    """Render as a clickable link only when it is unambiguously a
    LinkedIn https URL — the value originates from a vendor."""
    if url and url.startswith("https://") and ".linkedin.com/" in url + "/":
        return url
    if url and url.startswith("https://linkedin.com/"):
        return url
    return None


# ---------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return _page(request, "login.html", None, "login",
                 {"missing": config.missing_ui_vars(), "error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    check_origin(request)
    missing = config.missing_ui_vars()
    if missing:
        return _page(request, "login.html", None, "login",
                     {"missing": missing, "error": None}, status_code=503)

    form = await request.form()
    email = str(form.get("email") or "").strip().lower()
    password = str(form.get("password") or "")

    try:
        ok = email and password and await supabase_auth.password_grant(email, password)
    except ProviderError as exc:
        log.error("login: %s", exc)
        return _page(request, "login.html", None, "login",
                     {"missing": [], "error": "Sign-in service unavailable — try again shortly."},
                     status_code=502)

    if not ok:
        return _page(request, "login.html", None, "login",
                     {"missing": [], "error": "Invalid email or password."},
                     status_code=401)

    response = RedirectResponse(safe_next(request.query_params.get("next")), status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        sign_session(email),
        max_age=config.SESSION_MAX_AGE_MINUTES * 60,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/ui",
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    check_origin(request)
    response = RedirectResponse("/ui/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/ui")
    return response


# ---------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: Session = Depends(require_session)):
    return _page(request, "dashboard.html", session, "dashboard",
                 {"stages": runs.STAGES})


@router.get("/fragments/stats", response_class=HTMLResponse)
async def stats_fragment(request: Request, session: Session = Depends(require_session)):
    from .. import emailer, scheduler

    consents = sorted(emailer.build_senders())
    counts = await repo.status_counts()
    email_counts = await repo.email_status_counts()
    review = await repo.review_counts()
    verify_n = await repo.count_verify_queue()
    errors_24h, events_24h = await repo.events_error_stats_24h()

    # KPI + funnel arithmetic on repo results (no SQL here). "Enriched"
    # means past the enrichment stage; "sent" counts every contact whose
    # first-touch email went out, whatever happened to it afterwards.
    total = sum(counts.values())
    enriched = sum(counts.get(s, 0) for s in
                   ("enriched", "scraping", "ready_to_draft", "drafting", "drafted"))
    drafted = counts.get("drafted", 0)
    approved = review.get("approved", 0)
    sent = sum(email_counts.get(s, 0) for s in ("sent_email", "replied", "bounced"))
    replied = email_counts.get("replied", 0)

    def pct(n: int) -> int:
        return round(n / total * 100) if total else 0

    return templates.TemplateResponse(request, "fragments/_stats.html", {
        "kpi": {
            "total": total,
            "enriched": enriched,
            "drafted": drafted,
            "approved": approved,
            "sent": sent,
            "reply_rate": round(replied / sent * 100, 1) if sent else None,
            "verify_n": verify_n,
            "review_n": review.get("pending_review", 0),
            "error_rate": round(errors_24h / events_24h * 100, 1) if events_24h else 0.0,
        },
        "funnel": [
            ("Contacts", total, pct(total)),
            ("Enriched", enriched, pct(enriched)),
            ("Drafted", drafted, pct(drafted)),
            ("Approved", approved, pct(approved)),
            ("Sent", sent, pct(sent)),
        ],
        "campaigns": sorted(await repo.list_campaigns(),
                            key=lambda c: c["contacts"], reverse=True)[:5],
        "email_counts": email_counts,
        "runs": runs.status(),
        "stuck_sending": await repo.count_stuck_sending(),
        "unsendable": await repo.unsendable_approved_counts(consents),
        "scheduler": scheduler.info(),
    })


@router.get("/fragments/events", response_class=HTMLResponse)
async def events_fragment(request: Request, errors: int = 0,
                          session: Session = Depends(require_session)):
    events = await repo.recent_events(limit=30, only_errors=bool(errors))
    for event in events:
        payload = event.get("payload")
        event["payload_text"] = json.dumps(payload, default=str) if payload else ""
    return templates.TemplateResponse(request, "fragments/_events.html",
                                      {"events": events, "errors": bool(errors)})


@router.post("/runs/{stage}", response_class=HTMLResponse)
async def trigger_run(request: Request, stage: str,
                      session: Session = Depends(require_session)):
    check_origin(request)
    if stage not in runs.STAGES:
        raise HTTPException(status_code=404)
    missing = runs.missing_config(stage)
    if missing:
        message = f"not configured — missing {', '.join(missing)}"
    elif runs.try_start(stage):
        message = "started"
    else:
        message = "already running"
    return templates.TemplateResponse(request, "fragments/_run_button.html", {
        "stage": stage,
        "running": runs.status()[stage],
        "message": message,
    })


# ---------------------------------------------------------------------
# enrichment verification
# ---------------------------------------------------------------------


@router.get("/verify", response_class=HTMLResponse)
async def verify_page(request: Request, campaign_id: int | None = None,
                      session: Session = Depends(require_session)):
    queue = await repo.enrichment_review_queue(campaign_id)
    for contact in queue:
        contact["safe_url"] = safe_linkedin_url(contact["linkedin_url"])
    return _page(request, "verify.html", session, "verify", {
        "queue": queue,
        "outcomes": await repo.verification_outcomes(),
        "campaigns": await repo.list_campaigns(),
        "campaign_id": campaign_id,
    })


@router.post("/contacts/{contact_id}/enrichment/{verdict}", response_class=HTMLResponse)
async def enrichment_verdict(request: Request, contact_id: int, verdict: str,
                             session: Session = Depends(require_session)):
    check_origin(request)
    if verdict == "confirm":
        ok = await repo.confirm_enrichment(contact_id, session.email)
        outcome = "Confirmed — right person."
    elif verdict == "reject":
        ok = await repo.reject_enrichment(contact_id, session.email)
        outcome = "Rejected — URL, profile and drafts cleared; contact re-queued (email-only)."
    else:
        raise HTTPException(status_code=404)
    if not ok:
        outcome = "No change — contact was mid-run or already cleared. Refresh the page."
    return templates.TemplateResponse(request, "fragments/_verify_row.html", {
        "ok": ok and verdict == "confirm",
        "outcome": outcome,
    })


# ---------------------------------------------------------------------
# draft review
# ---------------------------------------------------------------------


@router.get("/review", response_class=HTMLResponse)
async def review_page(request: Request, status: str = "pending_review",
                      campaign_id: int | None = None,
                      session: Session = Depends(require_session)):
    if status not in ("pending_review", "approved", "rejected"):
        raise HTTPException(status_code=400, detail="unknown review status")
    queue = await repo.review_queue(status, campaign_id)
    for contact in queue:
        _decorate_contact(contact)
    return _page(request, "review.html", session, "review", {
        "queue": queue,
        "status": status,
        "counts": await repo.review_counts(),
        "campaigns": await repo.list_campaigns(),
        "campaign_id": campaign_id,
    })


def _decorate_contact(contact: dict) -> dict:
    contact["safe_url"] = safe_linkedin_url(contact.get("linkedin_url"))
    profile = contact.get("profile_data")
    contact["profile_pretty"] = (
        json.dumps(profile, indent=2, ensure_ascii=False, default=str)[:4000]
        if profile else "(no profile — template draft)"
    )
    return contact


# One route for save/approve/reject so approving always persists the
# edits sitting in the form — a separate approve endpoint would silently
# approve stale content whenever the reviewer forgot to hit Save first.
@router.post("/contacts/{contact_id}/draft", response_class=HTMLResponse)
async def save_draft(request: Request, contact_id: int,
                     session: Session = Depends(require_session)):
    check_origin(request)
    form = await request.form()
    action = str(form.get("action") or "save")
    subject = str(form.get("email_subject") or "").strip()
    body = str(form.get("email_body") or "").strip()
    note = str(form.get("linkedin_note") or "").strip() or None
    # The card carries the reviewer's current tab + campaign filter so a
    # verdict can re-render the tabs with the right active tab and scope.
    view = _review_view(form)

    def card(message: str, error: bool = False, status_code: int = 200):
        return _card_response(request, contact_id, message, error, status_code, view)

    if action not in ("save", "approve", "reject"):
        raise HTTPException(status_code=400, detail="unknown action")
    if note and len(note) > NOTE_MAX_CHARS:
        # Pre-checked so the DB CHECK constraint never surfaces as a 500.
        return await card(
            f"LinkedIn note is {len(note)} characters — the limit is {NOTE_MAX_CHARS}.",
            error=True, status_code=422)
    if not subject or not body:
        return await card("Subject and body cannot be empty.", error=True, status_code=422)

    if not await repo.update_draft(contact_id, subject, body, note, session.email):
        return await card("No change — contact is being re-drafted. Refresh the page.",
                          error=True)

    if action == "save":
        return await card("Saved.")
    verdict = "approved" if action == "approve" else "rejected"
    await repo.set_review_status(contact_id, verdict, session.email)
    if verdict == "approved":
        # Approval opens the send gate for this contact — start the email
        # run now so approved mail leaves in seconds, not next tick. The
        # gate itself is untouched: the run still claims only approved rows.
        runs.nudge("email")
    # A verdicted draft is no longer "awaiting review": collapse its card
    # to an acknowledgement and refresh the tab counts out-of-band, so the
    # reviewer keeps moving down the stack without reloading.
    return await _resolved_response(request, contact_id, f"Saved and {verdict}.", view)


def _review_view(form) -> dict:
    """The reviewer's current tab + campaign filter, read from the hidden
    fields the card posts back. Drives the active tab and scope when the
    tabs are re-rendered after an action."""
    raw_campaign = str(form.get("campaign_id") or "")
    return {
        "status": str(form.get("status") or "pending_review"),
        "campaign_id": int(raw_campaign) if raw_campaign.isdigit() else None,
    }


async def _card_response(request: Request, contact_id: int, message: str,
                         error: bool, status_code: int = 200,
                         view: dict | None = None):
    contact = await repo.contact_detail(contact_id)
    if contact is None:
        raise HTTPException(status_code=404)
    _decorate_contact(contact)
    view = view or {"status": "pending_review", "campaign_id": None}
    return templates.TemplateResponse(request, "fragments/_review_card.html", {
        "contact": contact,
        "message": message,
        "error": error,
        **view,
    }, status_code=status_code)


async def _resolved_response(request: Request, contact_id: int, message: str,
                             view: dict):
    contact = await repo.contact_detail(contact_id)
    if contact is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "fragments/_review_resolved.html", {
        "contact": contact,
        "message": message,
        "error": False,
        "counts": await repo.review_counts(),
        "oob": True,
        **view,
    })


@router.post("/contacts/{contact_id}/redraft", response_class=HTMLResponse)
async def redraft(request: Request, contact_id: int,
                  session: Session = Depends(require_session)):
    check_origin(request)
    view = _review_view(await request.form())
    if await repo.requeue_for_redraft(contact_id, session.email):
        message = "Re-queued for a fresh draft — it will reappear after the next draft run."
        error = False
    else:
        message = "No change — contact is not in the drafted state."
        error = True
    return await _card_response(request, contact_id, message, error, view=view)


# ---------------------------------------------------------------------
# campaigns
# ---------------------------------------------------------------------


@router.get("/campaigns", response_class=HTMLResponse)
async def campaigns_page(request: Request, session: Session = Depends(require_session)):
    return _page(request, "campaigns.html", session, "campaigns",
                 {"campaigns": await repo.list_campaigns()})


@router.get("/campaigns/new", response_class=HTMLResponse)
async def campaign_new(request: Request, session: Session = Depends(require_session)):
    return _page(request, "campaign_new.html", session, "campaigns",
                 {"form": {}, "error": None})


# Flash outcomes ride the redirect as WHITELISTED enum params only — no
# user-controlled text ever round-trips through the URL. Unknown values
# render nothing.
_BRIEF_FLASHES = {
    "llm": ("ok", "Campaign brief generated from your objective — review and refine below."),
    "fallback": ("warn", "Brief defaults used (LLM unavailable) — the objective was saved "
                         "as the offer description; refine the fields below."),
}
_SMARTLEAD_FLASHES = {
    "ok": ("ok", "Smartlead campaign created and fully configured: shell sequence, "
                 "mailboxes, schedule — active and ready to send."),
    "failed": ("error", "Smartlead setup failed — nothing was created. "
                        "Use “Set up Smartlead” below to retry."),
    "unconfigured": ("warn", "SMARTLEAD_API_KEY is not set — cold sends need it. "
                             "Set it and use “Set up Smartlead” below."),
    "exists": ("warn", "This campaign already has a Smartlead campaign id — "
                       "clear the field first to build a fresh one."),
    "partial-sequence": ("warn", "Smartlead campaign created, but saving the sequence failed — "
                                 "add the {{personalized_subject}} / {{personalized_body}} shell "
                                 "in Smartlead, then activate."),
    "partial-email-accounts": ("warn", "Smartlead campaign created, but attaching a mailbox failed "
                                       "(is one connected?) — attach it in Smartlead, then activate."),
    "partial-schedule": ("warn", "Smartlead campaign created, but setting the schedule failed — "
                                 "set it in Smartlead, then activate."),
    "partial-activate": ("warn", "Smartlead campaign configured but not activated — "
                                 "press Start in Smartlead."),
}
_INGEST_FLASHES = {
    "invalid": ("error", "The CSV could not be used — upload it again below."),
    "failed": ("error", "Contact ingestion is unreachable — the campaign is fine; "
                        "upload the CSV again below."),
}


def _campaign_flashes(request: Request) -> list[dict]:
    flashes = []
    params = request.query_params
    for key, table in (("brief", _BRIEF_FLASHES),
                       ("smartlead", _SMARTLEAD_FLASHES),
                       ("ingest", _INGEST_FLASHES)):
        entry = table.get(params.get(key, ""))
        if entry:
            flashes.append({"level": entry[0], "text": entry[1]})
    ingested = params.get("ingested", "")
    if ingested.isdigit():
        flashes.append({"level": "ok",
                        "text": f"{int(ingested)} contact(s) sent to ingestion."})
    return flashes


@router.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
async def campaign_edit(request: Request, campaign_id: int,
                        session: Session = Depends(require_session)):
    campaign = await repo.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404)
    return _page(request, "campaign_form.html", session, "campaigns",
                 {"campaign": campaign, "error": None,
                  "flashes": _campaign_flashes(request)})


async def _campaign_fields(request: Request) -> dict:
    form = await request.form()
    fields = {f: (str(form.get(f) or "").strip() or None) for f in repo.CAMPAIGN_FIELDS}
    fields["status"] = fields["status"] or "active"
    return fields


async def _setup_smartlead(name: str, campaign_id: int) -> str:
    """Run the Smartlead auto-setup and persist the id. Returns the flash
    enum. Never raises — a vendor failure must not fail campaign creation."""
    if not config.SMARTLEAD_API_KEY:
        return "unconfigured"
    try:
        setup = await smartlead.setup_campaign(name)
    except ProviderError as exc:
        log.error("smartlead setup failed for campaign %d: %s", campaign_id, exc)
        return "failed"
    # The id is written even on partial setup — it is what makes the
    # campaign sendable; the flash names the step to finish by hand.
    await repo.set_smartlead_campaign_id(campaign_id, setup.campaign_id)
    return f"partial-{setup.failed_step}" if setup.failed_step else "ok"


@router.post("/campaigns", response_class=HTMLResponse)
async def campaign_create(request: Request, session: Session = Depends(require_session)):
    """Quick create: name + objective + sender (+ optional CSV). The
    objective is LLM-expanded into the brief; Smartlead is built
    automatically; every vendor step is best-effort and reported via
    redirect flashes — only the DB insert can abort creation."""
    check_origin(request)
    form = await request.form()
    name = str(form.get("name") or "").strip()
    objective = str(form.get("objective") or "").strip()
    sender_name = str(form.get("sender_name") or "").strip() or None
    sender_role = str(form.get("sender_role") or "").strip() or None
    file = form.get("file")

    def invalid(message: str):
        return _page(request, "campaign_new.html", session, "campaigns", {
            "form": {"name": name, "objective": objective,
                     "sender_name": sender_name, "sender_role": sender_role},
            "error": message,
        }, status_code=422)

    if not name:
        return invalid("Name is required.")
    if not objective:
        return invalid("Objective is required — it becomes the campaign brief.")

    brief, brief_source = await campaign_brief.expand_objective(
        objective, sender_name, sender_role)

    # Values shaped by the live campaigns schema (verified 2026-08-10):
    # free-text brief columns are NOT NULL → empty strings when unknown;
    # channel_policy is NOT NULL + CHECK(linkedin_then_email |
    # email_then_linkedin | linkedin_only | email_only) → email_only,
    # because phase 1 sends email only (LinkedIn is phase 2; switch per
    # campaign on the edit page). sender_email / smartlead_campaign_id
    # stay NULL — the send claim reads NULL as "not configured".
    fields = {f: "" for f in repo.CAMPAIGN_FIELDS}
    fields.update({"sender_email": None, "smartlead_campaign_id": None})
    fields.update(brief)
    fields.update({
        "name": name,
        "status": "active",
        "consent_status": "cold",
        "channel_policy": "email_only",
        "sender_name": sender_name or "",
        "sender_role": sender_role or "",
    })
    try:
        campaign_id = await repo.create_campaign(fields)
    except asyncpg.PostgresError as exc:
        return invalid(str(exc))

    outcome = {"brief": brief_source,
               "smartlead": await _setup_smartlead(name, campaign_id)}

    if isinstance(file, UploadFile) and file.filename:
        try:
            rows, _problems = parse_contacts_csv(await file.read())
            if not rows:
                raise ValueError("no usable rows")
            await n8n.ingest(campaign_id, rows)
            outcome["ingested"] = str(len(rows))
            # Contacts just landed at 'pending' — start enrichment now
            # instead of waiting out the scheduler tick.
            runs.nudge("enrich")
        except ValueError:
            outcome["ingest"] = "invalid"
        except ProviderError as exc:
            log.error("quick-create csv ingest: %s", exc)
            outcome["ingest"] = "failed"

    query = "&".join(f"{k}={v}" for k, v in outcome.items())
    return RedirectResponse(f"/ui/campaigns/{campaign_id}?{query}", status_code=303)


@router.post("/campaigns/{campaign_id}/smartlead-setup")
async def campaign_smartlead_setup(request: Request, campaign_id: int,
                                   session: Session = Depends(require_session)):
    """Retry (or first-time, for pre-existing campaigns) Smartlead setup."""
    check_origin(request)
    campaign = await repo.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404)
    if campaign.get("smartlead_campaign_id"):
        # Re-running would create a duplicate Smartlead campaign; clearing
        # the field on the edit form is the explicit opt-in to rebuild.
        outcome = "exists"
    else:
        outcome = await _setup_smartlead(campaign["name"], campaign_id)
    return RedirectResponse(f"/ui/campaigns/{campaign_id}?smartlead={outcome}",
                            status_code=303)


@router.post("/campaigns/{campaign_id}", response_class=HTMLResponse)
async def campaign_update(request: Request, campaign_id: int,
                          session: Session = Depends(require_session)):
    check_origin(request)
    fields = await _campaign_fields(request)
    try:
        found = await repo.update_campaign(campaign_id, fields)
    except asyncpg.PostgresError as exc:
        fields["id"] = campaign_id
        return _page(request, "campaign_form.html", session, "campaigns",
                     {"campaign": fields, "error": str(exc)}, status_code=422)
    if not found:
        raise HTTPException(status_code=404)
    return RedirectResponse(f"/ui/campaigns/{campaign_id}", status_code=303)


@router.post("/campaigns/{campaign_id}/delete")
async def campaign_delete(request: Request, campaign_id: int,
                          session: Session = Depends(require_session)):
    """Delete a campaign and its contacts/drafts locally. The vendor
    Smartlead campaign is left untouched — deleting it is a separate,
    riskier action (see repo.delete_campaign). Idempotent: a missing
    campaign still redirects to the list."""
    check_origin(request)
    await repo.delete_campaign(campaign_id)
    return RedirectResponse("/ui/campaigns", status_code=303)


@router.post("/campaigns/{campaign_id}/upload", response_class=HTMLResponse)
async def campaign_upload(request: Request, campaign_id: int,
                          session: Session = Depends(require_session)):
    check_origin(request)

    def result(context: dict, status_code: int = 200):
        return templates.TemplateResponse(
            request, "fragments/_upload_result.html", context, status_code=status_code)

    form = await request.form()
    file = form.get("file")
    if not isinstance(file, UploadFile):
        return result({"error": "No file received."}, status_code=422)

    try:
        rows, problems = parse_contacts_csv(await file.read())
    except ValueError as exc:
        return result({"error": str(exc)}, status_code=422)
    if not rows:
        return result({"error": "No usable rows found.", "problems": problems},
                      status_code=422)

    try:
        outcome = await n8n.ingest(campaign_id, rows)
    except ProviderError as exc:
        log.error("csv upload: %s", exc)
        return result({
            "error": "Ingestion service unreachable — the pipeline itself is "
                     "unaffected; try again later.",
            "detail": str(exc),
            "problems": problems,
        }, status_code=502)

    # Contacts just landed at 'pending' — start enrichment immediately.
    runs.nudge("enrich")
    return result({"outcome": outcome, "sent": len(rows), "problems": problems})
