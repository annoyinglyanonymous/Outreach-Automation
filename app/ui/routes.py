"""All UI routes. Pages are full renders; fragments are htmx swap targets.

Every route except login sits behind require_session; every mutating
route also passes check_origin. No SQL and no vendor HTTP in this
module — repo.py and providers/ own those.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import asyncpg
from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
# Starlette's class, not fastapi.UploadFile: request.form() yields the
# base class, and isinstance against the fastapi subclass silently drops
# every upload.
from starlette.datastructures import UploadFile

from .. import campaign_brief, drafting, emailer, repo, runs, send_schedule
from ..config import config
from ..drafting import NOTE_MAX_CHARS
from ..providers import n8n, supabase_auth
from ..providers.base import ProviderError
from ..providers.mailjet import MailjetSender
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
    LinkedIn https URL — the value originates from a vendor.

    Compares the parsed host, not a substring: the previous check asked
    whether ".linkedin.com/" appeared anywhere in the string, so
    https://evil.example/.linkedin.com/pwn rendered as a LinkedIn link.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:      # malformed IPv6 literal, bad port
        return None
    if parts.scheme != "https":
        return None
    host = (parts.hostname or "").lower()
    # Bare domain, or any subdomain of it (www., np., and friends).
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
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
    # The pipeline runs itself (scheduler + completion nudges); the manual
    # per-stage Run buttons were removed. Running/idle state still shows in
    # the stats fragment's statusline.
    return _page(request, "dashboard.html", session, "dashboard")


@router.get("/fragments/stats", response_class=HTMLResponse)
async def stats_fragment(request: Request, session: Session = Depends(require_session)):
    from .. import scheduler

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
        "unsendable": await repo.unsendable_approved_counts(),
        "scheduler": scheduler.info(),
    })


@router.get("/fragments/review-count", response_class=HTMLResponse)
async def review_count_fragment(request: Request,
                                session: Session = Depends(require_session)):
    """The pending-review count as a bare integer, for the sidebar badge +
    desktop-alert poller in base.html (present on every page). Plain text so
    the client parses it directly; no template needed."""
    counts = await repo.review_counts()
    return HTMLResponse(str(counts.get("pending_review", 0)))


_TIME_FMT = "%a %b %d, %I:%M %p %Z"


@router.get("/schedule", response_class=HTMLResponse)
async def schedule_page(request: Request,
                        session: Session = Depends(require_session)):
    """The drip made visible: upcoming batches (which approved emails go in
    each, the projected From mailbox, and an estimated send time) plus a
    recent-sends log. Read-only — pure projection over the live queue."""
    params = request.query_params
    cid = int(params["campaign"]) if params.get("campaign", "").isdecimal() else None

    queue = await repo.approved_unsent_queue(cid)
    senders = await repo.active_senders_in_rotation_order()
    recent = await repo.recent_sends(cid)
    campaigns = await repo.list_campaigns()

    # The drip only sends automatically when the scheduler is on, the email
    # stage is in it, and the send window is enabled; otherwise batches only go
    # out on a manual trigger, so we show groupings without times.
    drip_active = (config.SCHEDULER_ENABLED and "email" in config.SCHEDULER_STAGES
                   and config.SEND_WINDOW_ENABLED)
    batch_size = min(len(senders), config.SEND_BATCH_SIZE)
    tz = ZoneInfo(config.SEND_WINDOW_TZ)
    now = datetime.now(tz)
    batches = send_schedule.plan_batches(
        queue, senders, batch_size=batch_size, now=now, drip_active=drip_active,
        start_hour=config.SEND_WINDOW_START_HOUR,
        end_hour=config.SEND_WINDOW_END_HOUR,
        weekdays_only=config.SEND_WINDOW_WEEKDAYS_ONLY,
        interval_min=config.SCHEDULER_INTERVAL_MINUTES)
    # Format times server-side (portable strftime; times shown in the send tz).
    for b in batches:
        b["at_label"] = b["at"].strftime(_TIME_FMT) if b["at"] else None
    for r in recent:
        sent = r.get("email_sent_at")
        r["sent_label"] = sent.astimezone(tz).strftime(_TIME_FMT) if sent else "—"

    return _page(request, "schedule.html", session, "schedule", {
        "batches": batches, "recent": recent, "senders": senders,
        "queue_total": len(queue), "batch_size": batch_size,
        "drip_active": drip_active, "campaigns": campaigns,
        "selected_campaign": cid, "tz": config.SEND_WINDOW_TZ,
        "start_hour": config.SEND_WINDOW_START_HOUR,
        "end_hour": config.SEND_WINDOW_END_HOUR,
        "weekdays_only": config.SEND_WINDOW_WEEKDAYS_ONLY,
        "interval": config.SCHEDULER_INTERVAL_MINUTES,
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


# ---------------------------------------------------------------------
# enrichment verification
# ---------------------------------------------------------------------


@router.get("/verify", response_class=HTMLResponse)
async def verify_page(request: Request, campaign_id: int | None = None,
                      session: Session = Depends(require_session)):
    queue = await repo.enrichment_review_queue(campaign_id)
    for contact in queue:
        contact["safe_url"] = safe_linkedin_url(contact["linkedin_url"])
    recent = await repo.recent_verifications()
    for row in recent:
        row["safe_url"] = safe_linkedin_url(row.get("linkedin_url"))
    return _page(request, "verify.html", session, "verify", {
        "queue": queue,
        "outcomes": await repo.verification_outcomes(),
        "recent": recent,
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
    extra = contact.get("extra_data")
    if profile:
        contact["profile_pretty"] = json.dumps(
            profile, indent=2, ensure_ascii=False, default=str)[:4000]
    elif extra:
        # A CSV-only draft: personalized from the sheet columns, not a scrape.
        contact["profile_pretty"] = (
            "(personalized from CSV columns)\n"
            + json.dumps(extra, indent=2, ensure_ascii=False, default=str)
        )[:4000]
    else:
        contact["profile_pretty"] = "(no profile — template draft)"
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
    if not await repo.set_review_status(contact_id, verdict, session.email):
        # The edit landed, but the contact left 'drafted' in between (a
        # concurrent re-draft). Collapsing the card here would tell the
        # reviewer a verdict was recorded when none was, and they would
        # move on down the stack believing it done.
        return await card("Saved, but the verdict did not apply — the contact "
                          "is being re-drafted. Refresh the page.", error=True)
    if verdict == "approved":
        # Approval opens the send gate for this contact. HOW it leaves depends
        # on the campaign's send mode (migration 016): an 'immediate' campaign
        # drains its whole approved queue right now, ignoring the drip's window
        # + pacing (runs.send_now -> emailer.send_campaign_now); a 'batch' one
        # feeds the business-hours drip exactly as before. Both are best-effort
        # background runs — the scheduler is the safety net either way, and the
        # send gate itself is untouched (only approved rows are ever claimed).
        ctx = await repo.contact_send_context(contact_id)
        if ctx and ctx.get("send_mode") == "immediate":
            runs.send_now(ctx["campaign_id"])
        else:
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
        # isdecimal, not isdigit — see _campaign_flashes.
        "campaign_id": int(raw_campaign) if raw_campaign.isdecimal() else None,
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
    # Refresh the pool from Mailjet first (best-effort, same as the edit page)
    # so a just-verified sender shows up in the "Sending mailbox" dropdown
    # here too, without a detour through the Senders page.
    await emailer.sync_pool()
    return _page(request, "campaign_new.html", session, "campaigns",
                 {"form": {}, "error": None,
                  # The pool for the optional "Sending mailbox" pin.
                  "senders": await repo.list_senders()})


# Flash outcomes ride the redirect as WHITELISTED enum params only — no
# user-controlled text ever round-trips through the URL. Unknown values
# render nothing.
_BRIEF_FLASHES = {
    "llm": ("ok", "Campaign brief generated from your objective — review and refine below."),
    "fallback": ("warn", "Brief defaults used (LLM unavailable) — the objective was saved "
                         "as the offer description; refine the fields below."),
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
                       ("ingest", _INGEST_FLASHES)):
        entry = table.get(params.get(key, ""))
        if entry:
            flashes.append({"level": entry[0], "text": entry[1]})
    # isdecimal, not isdigit: '²'.isdigit() is True while int('²') raises,
    # so a hand-edited ?ingested= reached the browser as a 500.
    ingested = params.get("ingested", "")
    if ingested.isdecimal():
        flashes.append({"level": "ok",
                        "text": f"{int(ingested)} contact(s) sent to ingestion."})
    # Send-now override result (emailer.send_campaign_now via /send-now).
    sent_now = params.get("sent_now", "")
    if sent_now.isdecimal():
        n = int(sent_now)
        flashes.append({"level": "ok", "text":
                        f"Sent {n} approved email(s) now — bypassed the drip window."}
                       if n else
                       {"level": "warn", "text":
                        "Nothing sent — no approved contacts are ready, or there's no "
                        "active sending mailbox (check the Senders page)."})
    return flashes


async def _verified_senders() -> tuple[list[str], str | None]:
    """(verified from-addresses, error). Best-effort: a Mailjet outage or
    unset keys degrade the sender dropdown to manual entry rather than
    breaking the edit page, so the ProviderError is returned, not raised.
    Only allowlisted addresses are offered (config.SENDER_ALLOWED_ADDRESSES)
    so the datalist can't suggest an address that could never send."""
    try:
        addresses = await MailjetSender().list_verified_senders()
        return [a for a in addresses if config.sender_allowed(a)], None
    except ProviderError as exc:
        log.warning("sender list: %s", exc)
        return [], str(exc)


@router.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
async def campaign_edit(request: Request, campaign_id: int,
                        session: Session = Depends(require_session)):
    campaign = await repo.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404)
    # Keep the pool current with Mailjet's verified senders so the readiness
    # card is right on first visit too (best-effort — a Mailjet hiccup just
    # falls back to the last-synced pool). The pool, not a per-campaign
    # field, decides whether a cold campaign can send.
    await emailer.sync_pool()
    return _page(request, "campaign_form.html", session, "campaigns",
                 {"campaign": campaign, "error": None,
                  "flashes": _campaign_flashes(request),
                  # The pool, offered in the "Sending mailbox" dropdown so a
                  # campaign can pin to one sender or rotate across all.
                  "senders": await repo.list_senders(),
                  # Sends rotate through the sender pool; the campaign is
                  # ready only when the pool has an active sender.
                  "pool_active": await repo.count_active_senders() > 0})


def _pinned_sender_id(value) -> int | None:
    """The 'Sending mailbox' choice as a bigint FK, or None to rotate. The
    empty option (rotate across the pool) and any non-numeric value both mean
    'no pin'; a numeric id is bound as an int so asyncpg accepts it (a bare
    string would raise). isdecimal, not isdigit — see the flash-param guard."""
    text = str(value or "").strip()
    return int(text) if text.isdecimal() else None


def _enrichment_mode(value) -> str:
    """The campaign's enrichment-mode choice, normalized to a CHECK-legal
    value. Anything but an explicit 'csv' is 'linkedin' (the default, full
    pipeline), so a blank or hand-edited post never trips the DB CHECK."""
    return "csv" if str(value or "").strip().lower() == "csv" else "linkedin"


def _send_mode(value) -> str:
    """The campaign's send-mode choice, normalized to a CHECK-legal value.
    Anything but an explicit 'immediate' is 'batch' (the default drip), so a
    blank or hand-edited post never trips the DB CHECK."""
    return "immediate" if str(value or "").strip().lower() == "immediate" else "batch"


async def _campaign_fields(request: Request) -> dict:
    # CAMPAIGN_UPDATE_FIELDS, not CAMPAIGN_FIELDS: smartlead_campaign_id is
    # kept out of the edit update (it is inert now that cold sends via
    # Mailjet, but excluding it avoids resurrecting the stale-revert bug).
    form = await request.form()
    fields = {f: (str(form.get(f) or "").strip() or None)
              for f in repo.CAMPAIGN_UPDATE_FIELDS}
    fields["status"] = fields["status"] or "active"
    # pinned_sender_id is a bigint FK, not free text — coerce off the generic
    # string pass so asyncpg binds an int (or NULL to rotate the pool).
    fields["pinned_sender_id"] = _pinned_sender_id(form.get("pinned_sender_id"))
    # enrichment_mode is CHECK'd — normalize so a bad post never hits the DB.
    fields["enrichment_mode"] = _enrichment_mode(form.get("enrichment_mode"))
    # send_mode is CHECK'd too — same normalization (default 'batch').
    fields["send_mode"] = _send_mode(form.get("send_mode"))
    return fields


def _brief_from_form(form) -> dict:
    """The aliased brief dict the drafter expects (DraftTarget.campaign
    shape), read from the edit form's field names. Bridges the two aliases
    baked into CLAIM_DRAFT_SQL: offer_description -> offer, sender_name ->
    sender; every other key keeps its name. Blank -> None so empty brief
    lines drop out of the prompt."""
    def g(name: str) -> str | None:
        return str(form.get(name) or "").strip() or None
    return {
        "offer": g("offer_description"),
        "cta": g("cta"),
        "tone": g("tone"),
        "sender": g("sender_name"),
        "sender_role": g("sender_role"),
        "audience_rationale": g("audience_rationale"),
        "fallback_email_subject": g("fallback_email_subject"),
        "fallback_email_body": g("fallback_email_body"),
        # So the preview picks the CSV vs LinkedIn drafting path (drafting
        # .preview_draft reads this from the brief).
        "enrichment_mode": _enrichment_mode(form.get("enrichment_mode")),
    }


def _oversize(file: UploadFile, max_bytes: int | None = None) -> int | None:
    """The upload's declared size when it exceeds the cap, else None. The cap
    defaults to the n8n path's CSV_MAX_BYTES; the CSV-only path passes its
    larger CSV_ONLY_MAX_BYTES.

    Checked before .read() so an oversized file is refused instead of
    being buffered first. The declared size is client-supplied, so
    parse_contacts_csv still re-checks the real length as the backstop —
    this only stops us paying for the transfer.
    """
    cap = config.CSV_MAX_BYTES if max_bytes is None else max_bytes
    declared = getattr(file, "size", None)
    if declared is not None and declared > cap:
        return declared
    return None


@router.post("/campaigns", response_class=HTMLResponse)
async def campaign_create(request: Request, session: Session = Depends(require_session)):
    """Quick create: name + objective + sender (+ optional CSV). The
    objective is LLM-expanded into the brief; only the DB insert can abort
    creation. Cold campaigns send via Mailjet from the campaign's
    sender_email — set it on the edit page before the campaign can send."""
    check_origin(request)
    form = await request.form()
    name = str(form.get("name") or "").strip()
    objective = str(form.get("objective") or "").strip()
    pinned_sender_id = _pinned_sender_id(form.get("pinned_sender_id"))
    enrichment_mode = _enrichment_mode(form.get("enrichment_mode"))
    send_mode = _send_mode(form.get("send_mode"))
    file = form.get("file")
    # The pool for the "Sending mailbox" dropdown — fetched up front so an
    # invalid() re-render keeps it populated with the pin choice selected.
    senders = await repo.list_senders()

    def invalid(message: str):
        return _page(request, "campaign_new.html", session, "campaigns", {
            "form": {"name": name, "objective": objective,
                     "pinned_sender_id": pinned_sender_id,
                     "enrichment_mode": enrichment_mode,
                     "send_mode": send_mode},
            "error": message, "senders": senders,
        }, status_code=422)

    if not name:
        return invalid("Name is required.")
    if not objective:
        return invalid("Objective is required — it becomes the campaign brief.")

    # No per-campaign sender identity any more — the From name and sign-off
    # come from the sending mailbox, so the brief expansion gets no name/role.
    brief, brief_source = await campaign_brief.expand_objective(
        objective, None, None)

    # Values shaped by the live campaigns schema (verified 2026-08-10):
    # free-text brief columns are NOT NULL → empty strings when unknown;
    # channel_policy is NOT NULL + CHECK(linkedin_then_email |
    # email_then_linkedin | linkedin_only | email_only) → email_only,
    # because phase 1 sends email only (LinkedIn is phase 2; switch per
    # campaign on the edit page). sender_email stays NULL — the send claim
    # reads NULL as "not configured", so a cold campaign is unsendable
    # (surfaced, not silently dropped) until its verified Mailjet
    # sender_email is set on the edit page. smartlead_campaign_id is inert.
    fields = {f: "" for f in repo.CAMPAIGN_FIELDS}
    # NULLs, not "": these are non-text columns (FK / id). pinned_sender_id
    # is the operator's "Sending mailbox" choice (NULL = rotate the pool).
    fields.update({"sender_email": None, "smartlead_campaign_id": None,
                   "pinned_sender_id": pinned_sender_id})
    fields.update(brief)
    fields.update({
        "name": name,
        "status": "active",
        "consent_status": "cold",
        "channel_policy": "email_only",
        # sender_name/sender_role stay "" (the CAMPAIGN_FIELDS seed) — the
        # form no longer collects them; identity comes from the mailbox.
        # 'linkedin' (default) | 'csv' — must be a valid value, not the "" the
        # CAMPAIGN_FIELDS seed set (the column is CHECK'd + NOT NULL).
        "enrichment_mode": enrichment_mode,
        # Send mode from the form (default 'batch'); _send_mode normalizes it,
        # since the "" the CAMPAIGN_FIELDS seed set would trip the CHECK.
        "send_mode": send_mode,
    })
    try:
        campaign_id = await repo.create_campaign(fields)
    except asyncpg.PostgresError as exc:
        return invalid(str(exc))

    outcome = {"brief": brief_source}

    if isinstance(file, UploadFile) and file.filename:
        try:
            cap = (config.CSV_ONLY_MAX_BYTES if enrichment_mode == "csv"
                   else config.CSV_MAX_BYTES)
            if _oversize(file, cap) is not None:
                raise ValueError("file exceeds size cap")
            data = await file.read()
            if enrichment_mode == "csv":
                # Direct in-repo insert (captures all columns, lands at
                # ready_to_draft) — no n8n, no enrich/scrape/verify.
                rows, _problems = parse_contacts_csv(
                    data, keep_extras=True, max_bytes=cap,
                    max_rows=config.CSV_ONLY_MAX_ROWS)
                if not rows:
                    raise ValueError("no usable rows")
                res = await repo.insert_csv_contacts(campaign_id, rows)
                outcome["ingested"] = str(res["inserted"])
                runs.nudge("draft")
            else:
                rows, _problems = parse_contacts_csv(data)
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


@router.post("/campaigns/{campaign_id}/preview", response_class=HTMLResponse)
async def campaign_preview(request: Request, campaign_id: int,
                          session: Session = Depends(require_session)):
    """Draft one email for a real contact in this campaign from the brief
    currently on screen, so the operator can tune tone before the pipeline
    drafts hundreds. Read-only — it reads the posted (possibly unsaved) brief
    values and one contact, writes nothing, and saves nothing. Uses the same
    drafting path as production. Falls back to a synthetic sample when the
    campaign has no contacts ingested yet."""
    check_origin(request)
    form = await request.form()
    campaign = _brief_from_form(form)
    contact = await repo.get_preview_contact(campaign_id)
    preview = await drafting.preview_draft(campaign, contact=contact)
    # The signature is appended at SEND time keyed on the From, which for a
    # pinned campaign is one known mailbox. Resolve it here (a repo lookup —
    # kept out of drafting.preview_draft, which stays DB-free) so the preview
    # shows the same read-only sign-off the review card does. Rotating (no pin)
    # leaves both None and the fragment shows the "varies per send" note.
    pinned_id = _pinned_sender_id(form.get("pinned_sender_id"))
    preview["pinned_sender_id"] = pinned_id
    preview["signature"] = None
    if pinned_id is not None:
        sender = await repo.get_sender(pinned_id)
        preview["signature"] = sender.get("signature") if sender else None
    return templates.TemplateResponse(request, "fragments/_preview_result.html",
                                      {"preview": preview})


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
                     {"campaign": fields, "error": str(exc),
                      # Keep the "Sending mailbox" dropdown populated on the
                      # re-render so the operator's pin choice still shows.
                      "senders": await repo.list_senders()}, status_code=422)
    if not found:
        raise HTTPException(status_code=404)
    return RedirectResponse(f"/ui/campaigns/{campaign_id}", status_code=303)


@router.post("/campaigns/{campaign_id}/send-now")
async def campaign_send_now(request: Request, campaign_id: int,
                            session: Session = Depends(require_session)):
    """Manual override: send every approved-but-unsent contact in this
    campaign immediately, bypassing the business-hours drip + 5-minute pacing
    (all other safeguards stay — see emailer.send_campaign_now). For warming a
    mailbox or a small urgent batch. Redirects back with a count flash."""
    check_origin(request)
    campaign = await repo.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404)
    stats = await emailer.send_campaign_now(campaign_id)
    return RedirectResponse(
        f"/ui/campaigns/{campaign_id}?sent_now={stats.sent}", status_code=303)


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

    # The campaign's mode decides the parser, cap, and ingest path.
    campaign = await repo.get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404)
    mode = campaign.get("enrichment_mode") or "linkedin"
    cap = config.CSV_ONLY_MAX_BYTES if mode == "csv" else config.CSV_MAX_BYTES

    declared = _oversize(file, cap)
    if declared is not None:
        return result({"error": f"File is {declared} bytes; the limit is "
                                f"{cap}."}, status_code=422)

    data = await file.read()
    try:
        if mode == "csv":
            rows, problems = parse_contacts_csv(
                data, keep_extras=True, max_bytes=cap,
                max_rows=config.CSV_ONLY_MAX_ROWS)
        else:
            rows, problems = parse_contacts_csv(data)
    except ValueError as exc:
        return result({"error": str(exc)}, status_code=422)
    if not rows:
        return result({"error": "No usable rows found.", "problems": problems},
                      status_code=422)

    if mode == "csv":
        # Direct insert: captures every column, lands at ready_to_draft
        # (skips enrich/scrape/verify), and drafts from the sheet.
        summary = await repo.insert_csv_contacts(campaign_id, rows)
        runs.nudge("draft")
        return result({"csv": summary, "problems": problems})

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


# ---------------------------------------------------------------------
# sender pool (Mailjet From-rotation for cold)
# ---------------------------------------------------------------------


def _address_error() -> str:
    """The message shown when a sender address is outside the allowlist."""
    return ("Sender address not allowed — cold sends may only come from an "
            "approved address: " + ", ".join(config.SENDER_ALLOWED_ADDRESSES) + ".")


def _sender_fields(form) -> dict:
    """Parse the sender form. daily_cap falls back to the configured default
    on a blank/garbage value; active is a checkbox (absent = unchecked)."""
    try:
        cap = max(0, int(str(form.get("daily_cap") or "").strip()))
    except ValueError:
        cap = config.MAILJET_SENDER_DAILY_CAP
    return {
        "sender_email": str(form.get("sender_email") or "").strip() or None,
        "sender_name": str(form.get("sender_name") or "").strip() or None,
        "active": str(form.get("active") or "").strip().lower()
        in ("1", "true", "on", "yes"),
        "daily_cap": cap,
        # The per-address signature block appended at send time; blank = none.
        "signature": str(form.get("signature") or "").strip() or None,
    }


def _sender_form_page(request: Request, session: Session, sender,
                      error: str | None = None, verified: list[str] | None = None,
                      sender_error: str | None = None, status_code: int = 200):
    return _page(request, "sender_form.html", session, "senders", {
        "sender": sender, "error": error,
        "verified": verified or [], "sender_error": sender_error,
        "default_cap": config.MAILJET_SENDER_DAILY_CAP,
        "allowed_addresses": config.SENDER_ALLOWED_ADDRESSES,
    }, status_code=status_code)


@router.get("/senders", response_class=HTMLResponse)
async def senders_page(request: Request, session: Session = Depends(require_session)):
    # Loading the page IS the sync: the pool auto-enrols from Mailjet's
    # verified senders (and the "Sync from Mailjet" button just reloads).
    # Best-effort — sync_pool swallows a Mailjet outage and the last-synced
    # pool still renders, with a banner explaining the skip.
    sync = await emailer.sync_pool()
    return _page(request, "senders.html", session, "senders",
                 {"senders": await repo.list_senders(), "sync": sync,
                  # Set by sender_toggle when it refused to resume a
                  # non-allowlisted sender (?blocked=1).
                  "blocked": _address_error()
                  if request.query_params.get("blocked") else None})


@router.get("/senders/new", response_class=HTMLResponse)
async def sender_new(request: Request, session: Session = Depends(require_session)):
    verified, sender_error = await _verified_senders()
    return _sender_form_page(request, session, None,
                             verified=verified, sender_error=sender_error)


@router.post("/senders", response_class=HTMLResponse)
async def sender_create(request: Request, session: Session = Depends(require_session)):
    check_origin(request)
    fields = _sender_fields(await request.form())
    if not fields["sender_email"]:
        return _sender_form_page(request, session, fields,
                                 error="A sender email is required.", status_code=422)
    if not config.sender_allowed(fields["sender_email"]):
        return _sender_form_page(request, session, fields,
                                 error=_address_error(), status_code=422)
    try:
        await repo.create_sender(fields)
    except asyncpg.UniqueViolationError:
        return _sender_form_page(request, session, fields,
                                 error="That address is already in the pool.",
                                 status_code=422)
    return RedirectResponse("/ui/senders", status_code=303)


@router.get("/senders/{sender_id}", response_class=HTMLResponse)
async def sender_edit(request: Request, sender_id: int,
                      session: Session = Depends(require_session)):
    sender = await repo.get_sender(sender_id)
    if sender is None:
        raise HTTPException(status_code=404)
    return _sender_form_page(request, session, sender)


@router.post("/senders/{sender_id}", response_class=HTMLResponse)
async def sender_update(request: Request, sender_id: int,
                        session: Session = Depends(require_session)):
    check_origin(request)
    fields = _sender_fields(await request.form())
    stale = {**fields, "id": sender_id}
    if not fields["sender_email"]:
        return _sender_form_page(request, session, stale,
                                 error="A sender email is required.", status_code=422)
    if not config.sender_allowed(fields["sender_email"]):
        return _sender_form_page(request, session, stale,
                                 error=_address_error(), status_code=422)
    try:
        await repo.update_sender(sender_id, fields)
    except asyncpg.UniqueViolationError:
        return _sender_form_page(request, session, stale,
                                 error="That address is already in the pool.",
                                 status_code=422)
    return RedirectResponse("/ui/senders", status_code=303)


@router.post("/senders/{sender_id}/toggle")
async def sender_toggle(request: Request, sender_id: int,
                        session: Session = Depends(require_session)):
    check_origin(request)
    sender = await repo.get_sender(sender_id)
    if sender is None:
        raise HTTPException(status_code=404)
    activating = not sender["active"]
    # Never resume a non-allowlisted sender back into rotation — pausing is
    # always fine, activating is the dangerous direction. Surface why (a silent
    # no-op would look like the button was broken).
    if activating and not config.sender_allowed(sender["sender_email"]):
        return RedirectResponse("/ui/senders?blocked=1", status_code=303)
    await repo.set_sender_active(sender_id, activating)
    return RedirectResponse("/ui/senders", status_code=303)


@router.post("/senders/{sender_id}/delete")
async def sender_delete(request: Request, sender_id: int,
                        session: Session = Depends(require_session)):
    check_origin(request)
    await repo.delete_sender(sender_id)
    return RedirectResponse("/ui/senders", status_code=303)
