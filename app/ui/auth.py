"""Session cookies and route guards for the UI. No vendor HTTP here —
the credential check lives in providers/supabase_auth.py."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from ..config import config

COOKIE_NAME = "outreach_session"


@dataclass(slots=True)
class Session:
    email: str


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.SESSION_SECRET, salt="ui-session")


def sign_session(email: str) -> str:
    return _serializer().dumps({"email": email})


def read_session(cookie: str | None) -> str | None:
    if not cookie:
        return None
    try:
        # Expiry is enforced here, server-side: the cookie's Max-Age is
        # advisory and anything client-held can be replayed.
        data = _serializer().loads(
            cookie, max_age=config.SESSION_MAX_AGE_MINUTES * 60
        )
    except BadSignature:  # SignatureExpired subclasses BadSignature
        return None
    return data.get("email")


async def require_session(request: Request) -> Session:
    email = read_session(request.cookies.get(COOKIE_NAME))
    if email:
        return Session(email=email)
    # htmx swaps response bodies into page fragments, so a redirect body
    # would render a login page inside a table row; HX-Redirect makes
    # htmx navigate the whole window instead.
    if request.headers.get("HX-Request") == "true":
        raise HTTPException(status_code=401, headers={"HX-Redirect": "/ui/login"})
    raise HTTPException(status_code=303, headers={"Location": "/ui/login"})


def check_origin(request: Request) -> None:
    """Belt and braces on mutating routes: SameSite=Lax already blocks
    cross-site form POSTs in current browsers."""
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site and fetch_site not in ("same-origin", "none"):
        raise HTTPException(status_code=403, detail="cross-origin request rejected")
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host", ""):
        raise HTTPException(status_code=403, detail="cross-origin request rejected")


def safe_next(next_url: str | None) -> str:
    """Post-login redirect target, restricted so ?next= cannot become an
    open redirect."""
    if next_url and next_url.startswith("/ui/") and not next_url.startswith("//"):
        return next_url
    return "/ui/"
