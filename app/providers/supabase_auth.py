"""UI credential check. Only this module speaks to Supabase Auth (GoTrue).

The app never acts on Supabase with the user's token — repo.py talks to
Postgres directly — so the GoTrue tokens are discarded after the check
and the app issues its own session cookie (app/ui/auth.py). That keeps
JWT verification machinery out of the codebase entirely.
"""
from __future__ import annotations

import logging

import httpx

from ..config import config
from .base import ProviderError

log = logging.getLogger(__name__)


async def password_grant(email: str, password: str) -> bool:
    """True on valid credentials, False on invalid ones.

    ProviderError means Supabase itself was unreachable or broken —
    "auth is down" must never look like "wrong password".
    """
    url = f"{config.SUPABASE_URL}/auth/v1/token?grant_type=password"

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            response = await client.post(
                url,
                headers={
                    "apikey": config.SUPABASE_ANON_KEY,
                    "Content-Type": "application/json",
                },
                json={"email": email, "password": password},
            )
        except httpx.RequestError as exc:
            raise ProviderError(f"supabase auth: transport: {exc}") from exc

    if response.status_code == 200:
        return True
    if response.status_code in (400, 401, 403):
        return False
    raise ProviderError(
        f"supabase auth: {response.status_code} {response.text[:200]}"
    )
