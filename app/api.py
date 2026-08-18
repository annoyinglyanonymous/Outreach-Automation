from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles

from . import db, repo, runs, scheduler
from .config import config
from .ui import UI_DIR
from .ui import router as ui_router

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.validate()
    await db.init_pool()
    scheduler.start()
    yield
    scheduler.shutdown()
    await db.close_pool()


app = FastAPI(title="outreach-automation", lifespan=lifespan)
app.include_router(ui_router)
app.mount("/ui/static", StaticFiles(directory=str(UI_DIR / "static")), name="ui_static")


def _require_key(x_api_key: str | None) -> None:
    if not config.API_KEY or x_api_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing x-api-key")


def _start_or_raise(stage: str) -> dict:
    missing = runs.missing_config(stage)
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"{stage} not configured, missing: {', '.join(missing)}",
        )
    if not runs.try_start(stage):
        raise HTTPException(
            status_code=409, detail=f"a {stage} run is already in progress"
        )
    return {"status": "accepted"}


# No payload by design: each runner claims its own work. Accepting
# contact ids would couple the stages and let a duplicate trigger
# reference rows that are already claimed.
@app.post("/enrich/run", status_code=202)
async def enrich_run(x_api_key: str | None = Header(default=None)) -> dict:
    _require_key(x_api_key)
    return _start_or_raise("enrich")


@app.post("/scrape/run", status_code=202)
async def scrape_run(x_api_key: str | None = Header(default=None)) -> dict:
    _require_key(x_api_key)
    return _start_or_raise("scrape")


@app.post("/draft/run", status_code=202)
async def draft_run(x_api_key: str | None = Header(default=None)) -> dict:
    _require_key(x_api_key)
    return _start_or_raise("draft")


@app.post("/email/run", status_code=202)
async def email_run(x_api_key: str | None = Header(default=None)) -> dict:
    _require_key(x_api_key)
    return _start_or_raise("email")


@app.get("/")
async def root() -> dict:
    return {
        "service": "outreach-automation",
        "endpoints": {
            "GET /ui/": "web interface (browser login)",
            "GET /health": "liveness + database reachability",
            "GET /stats": "queue counts (requires x-api-key header)",
            "POST /enrich/run": "start an enrichment run (requires x-api-key header)",
            "POST /scrape/run": "collect finished Apify runs, start new ones (requires x-api-key header)",
            "POST /draft/run": "draft emails + LinkedIn notes for scraped contacts (requires x-api-key header)",
            "POST /email/run": "send approved drafts via Mailjet, rotating the From across the sender pool (requires x-api-key header)",
        },
    }


@app.get("/health")
async def health() -> dict:
    if not await db.healthcheck():
        raise HTTPException(status_code=503, detail="database unreachable")
    return {"ok": True}


@app.get("/stats")
async def stats(x_api_key: str | None = Header(default=None)) -> dict:
    _require_key(x_api_key)
    return {
        "linkedin_status": await repo.status_counts(),
        "email_status": await repo.email_status_counts(),
        "review": await repo.review_counts(),
        "apify_runs_in_flight": len(await repo.pending_runs()),
        "stuck_sending": await repo.count_stuck_sending(),
        "unsendable": await repo.unsendable_approved_counts(),
        "runs": runs.status(),
        "scheduler": scheduler.info(),
    }
