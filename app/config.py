from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Local development reads .env from the project root; real environment
# variables always win, so deployed containers are unaffected by it.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


class Config:
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    DB_POOL_MIN: int = _int("DB_POOL_MIN", 1)
    DB_POOL_MAX: int = _int("DB_POOL_MAX", 5)
    BATCH_SIZE: int = _int("BATCH_SIZE", 100)
    # Caps a single run so a bug cannot spin forever burning API credit.
    MAX_PASSES: int = _int("MAX_PASSES", 20)
    STALE_CLAIM_MINUTES: int = _int("STALE_CLAIM_MINUTES", 15)
    CACHE_MAX_AGE_DAYS: int = _int("CACHE_MAX_AGE_DAYS", 365)
    CACHE_MIN_CONFIDENCE: float = float(os.getenv("CACHE_MIN_CONFIDENCE", "0.70"))
    PROVIDER: str = os.getenv("PROVIDER", "apollo")

    APOLLO_API_KEY: str = os.getenv("APOLLO_API_KEY", "")
    APOLLO_BATCH_SIZE: int = _int("APOLLO_BATCH_SIZE", 10)
    APOLLO_CHUNK_DELAY_SECONDS: float = float(os.getenv("APOLLO_CHUNK_DELAY_SECONDS", "1.0"))
    MIN_ACCEPT_CONFIDENCE: float = float(os.getenv("MIN_ACCEPT_CONFIDENCE", "0.55"))

    PROVIDER_TIMEOUT_SECONDS: int = _int("PROVIDER_TIMEOUT_SECONDS", 60)
    PROVIDER_MAX_RETRIES: int = _int("PROVIDER_MAX_RETRIES", 4)

    # --- apify (stage 2: profile scraping) --------------------------------
    APIFY_TOKEN: str = os.getenv("APIFY_TOKEN", "")
    # Must be a cookieless actor. Actors that need a LinkedIn session
    # cookie put the sending accounts at ban risk and are the pattern
    # that got Proxycurl shut down.
    APIFY_ACTOR_ID: str = os.getenv("APIFY_ACTOR_ID", "")
    # Actors disagree on input shape, so the key that carries the URL
    # array is config: switching actors is an env change, not a code change.
    APIFY_INPUT_KEY: str = os.getenv("APIFY_INPUT_KEY", "profileUrls")
    # Extra actor input merged into the run payload verbatim (a JSON
    # object), e.g. proxy or output options some actors require.
    APIFY_EXTRA_INPUT: str = os.getenv("APIFY_EXTRA_INPUT", "")
    # Dataset item keys tried in order when matching results back to
    # contacts — again, actor-dependent.
    APIFY_URL_FIELDS: tuple[str, ...] = tuple(
        f.strip()
        for f in os.getenv(
            "APIFY_URL_FIELDS", "url,profileUrl,linkedinUrl,inputUrl,publicUrl"
        ).split(",")
        if f.strip()
    )
    SCRAPE_BATCH_SIZE: int = _int("SCRAPE_BATCH_SIZE", 50)
    # Runs already in flight count against this cap, so a trigger loop
    # cannot pile up concurrent actor runs and burn credit unbounded.
    APIFY_MAX_ACTIVE_RUNS: int = _int("APIFY_MAX_ACTIVE_RUNS", 3)

    # --- drafting (stage 3) -----------------------------------------------
    # Which vendor writes the drafts: "n8n" (an n8n webhook fronting the
    # org's OpenAI credential) or "anthropic". Both speak through the same
    # Drafter protocol, so this is the whole switch. Only n8n exposes the
    # complete_json that verification and brief-expansion also reuse.
    DRAFT_PROVIDER: str = os.getenv("DRAFT_PROVIDER", "n8n")
    # The n8n LLM webhook (docs/n8n-llm-workflow.json): takes
    # {system, user}, answers with the model's JSON object.
    N8N_LLM_URL: str = os.getenv("N8N_LLM_URL", "")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    # Empty means the provider's own default (anthropic: claude-opus-4-8);
    # the n8n workflow owns its model, so this does not apply to it.
    DRAFT_MODEL: str = os.getenv("DRAFT_MODEL", "")
    DRAFT_EFFORT: str = os.getenv("DRAFT_EFFORT", "high")
    # Bounds generation for the anthropic provider; the n8n workflow owns
    # its own generation limits and ignores this.
    DRAFT_MAX_TOKENS: int = _int("DRAFT_MAX_TOKENS", 4000)
    DRAFT_BATCH_SIZE: int = _int("DRAFT_BATCH_SIZE", 25)
    # Cap on the profile JSON passed into the prompt; beyond this the
    # marginal personalisation value does not justify the input tokens.
    DRAFT_PROFILE_CHAR_LIMIT: int = _int("DRAFT_PROFILE_CHAR_LIMIT", 6000)

    # --- AI verification (between scrape and draft) -----------------------
    # An LLM judges whether the LinkedIn profile a vendor matched really
    # belongs to the contact; a wrong match is rejected to the email-only
    # template path. Reuses the drafting LLM route (DRAFT_PROVIDER /
    # N8N_LLM_URL). Off makes the stage a no-op and manual /ui/verify the
    # path. gpt-4o-mini judges a match well under a cent per contact.
    VERIFY_ENABLED: bool = os.getenv(
        "VERIFY_ENABLED", "true"
    ).strip().lower() in ("1", "true", "yes", "on")
    VERIFY_BATCH_SIZE: int = _int("VERIFY_BATCH_SIZE", 25)

    # --- email sending (stage 4) -------------------------------------------
    # Mailjet is the only send provider; every approved draft goes out
    # through it, rotating its From across the global sender pool. Mailjet
    # uses a key/secret pair (HTTP Basic auth).
    MAILJET_API_KEY: str = os.getenv("MAILJET_API_KEY", "")
    MAILJET_SECRET_KEY: str = os.getenv("MAILJET_SECRET_KEY", "")
    # Default daily cap for a newly-added rotation sender (migration 010) —
    # a fresh, unwarmed domain's ramp starting point, raised per-sender over
    # the first weeks.
    MAILJET_SENDER_DAILY_CAP: int = _int("MAILJET_SENDER_DAILY_CAP", 25)
    SEND_BATCH_SIZE: int = _int("SEND_BATCH_SIZE", 25)

    # --- scheduler (automation) -------------------------------------------
    # Off by default: the pipeline stays fully manual until you opt in, so a
    # headless enrichment-only deploy or a local test never fires a stage on
    # its own. The scheduler drives only the queue stages — review/approval
    # is never automated, so a human still gates every send.
    SCHEDULER_ENABLED: bool = os.getenv(
        "SCHEDULER_ENABLED", ""
    ).strip().lower() in ("1", "true", "yes", "on")
    SCHEDULER_INTERVAL_MINUTES: int = _int("SCHEDULER_INTERVAL_MINUTES", 5)
    # Which stages the scheduler triggers. Drop 'email' to keep sending
    # manual while everything upstream runs unattended; the order is
    # irrelevant since each stage claims its own work from the queue.
    SCHEDULER_STAGES: tuple[str, ...] = tuple(
        s.strip()
        for s in os.getenv("SCHEDULER_STAGES", "enrich,scrape,verify,draft,email").split(",")
        if s.strip()
    )

    # --- ui ---------------------------------------------------------------
    # Supabase Auth (GoTrue) checks credentials; the app then issues its
    # own signed session cookie, so no JWT verification machinery exists.
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "").rstrip("/")
    SUPABASE_ANON_KEY: str = os.getenv("SUPABASE_ANON_KEY", "")
    SESSION_SECRET: str = os.getenv("SESSION_SECRET", "")
    SESSION_MAX_AGE_MINUTES: int = _int("SESSION_MAX_AGE_MINUTES", 480)
    # Contact ingestion stays canonical in the n8n webhook; the UI's CSV
    # upload proxies to it rather than duplicating the atomic insert.
    N8N_INGEST_URL: str = os.getenv("N8N_INGEST_URL", "")
    CSV_MAX_BYTES: int = _int("CSV_MAX_BYTES", 2_000_000)
    # CSV-only campaigns (enrichment_mode='csv') bypass n8n and insert
    # directly (repo.insert_csv_contacts), so they need headroom for a ~12k
    # sheet the n8n path's 2000-row / 2 MB caps can't hold. The chunk bounds
    # each INSERT transaction (the session pooler holds locks across
    # statements — see CLAUDE.md).
    CSV_ONLY_MAX_ROWS: int = _int("CSV_ONLY_MAX_ROWS", 20_000)
    CSV_ONLY_MAX_BYTES: int = _int("CSV_ONLY_MAX_BYTES", 20_000_000)
    CSV_INSERT_CHUNK: int = _int("CSV_INSERT_CHUNK", 500)

    # --- api ------------------------------------------------------------
    # Shared secret required on the mutating endpoints.
    API_KEY: str = os.getenv("API_KEY", "")

    @classmethod
    def validate(cls) -> None:
        missing = []
        if not cls.DATABASE_URL:
            missing.append("DATABASE_URL")
        if not cls.API_KEY:
            missing.append("API_KEY")
        if cls.PROVIDER == "apollo" and not cls.APOLLO_API_KEY:
            missing.append("APOLLO_API_KEY")
        if missing:
            raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

    @classmethod
    def missing_scrape_vars(cls) -> list[str]:
        """Checked at /scrape/run rather than startup, so the service can be
        deployed for enrichment before an Apify actor has been chosen."""
        return [
            name
            for name, value in (
                ("APIFY_TOKEN", cls.APIFY_TOKEN),
                ("APIFY_ACTOR_ID", cls.APIFY_ACTOR_ID),
            )
            if not value
        ]

    @classmethod
    def missing_draft_vars(cls) -> list[str]:
        """Same pattern as scraping: checked at /draft/run, not startup."""
        if cls.DRAFT_PROVIDER == "n8n":
            return [] if cls.N8N_LLM_URL else ["N8N_LLM_URL"]
        return [] if cls.ANTHROPIC_API_KEY else ["ANTHROPIC_API_KEY"]

    @classmethod
    def missing_verify_vars(cls) -> list[str]:
        """Non-empty means the verify stage is skipped (nudge + scheduler),
        so scrape's completion nudge walks past it straight to draft.
        Reuses the drafting LLM route — only n8n exposes the complete_json a
        verdict needs."""
        if not cls.VERIFY_ENABLED:
            return ["VERIFY_ENABLED (off)"]
        if cls.DRAFT_PROVIDER == "n8n":
            return [] if cls.N8N_LLM_URL else ["N8N_LLM_URL"]
        return ["DRAFT_PROVIDER=n8n (for verification)"]

    @classmethod
    def missing_email_vars(cls) -> list[str]:
        """Non-empty only when Mailjet — the sole send provider — isn't
        configured; the stage then no-ops."""
        if cls.MAILJET_API_KEY and cls.MAILJET_SECRET_KEY:
            return []
        return ["MAILJET_API_KEY+MAILJET_SECRET_KEY"]

    @classmethod
    def missing_ui_vars(cls) -> list[str]:
        """Checked at the login page, so headless deploys need none of it."""
        return [
            name
            for name, value in (
                ("SUPABASE_URL", cls.SUPABASE_URL),
                ("SUPABASE_ANON_KEY", cls.SUPABASE_ANON_KEY),
                ("SESSION_SECRET", cls.SESSION_SECRET),
            )
            if not value
        ]


config = Config()
