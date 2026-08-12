"""Session-cookie signing/verification and redirect-target hygiene."""
from __future__ import annotations

import pytest

from app.config import config
from app.ui import auth


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(config, "SESSION_MAX_AGE_MINUTES", 480)


def test_round_trip():
    token = auth.sign_session("rojan@example.com")
    assert auth.read_session(token) == "rojan@example.com"


def test_tampered_token_rejected():
    token = auth.sign_session("a@b.c")
    assert auth.read_session(token[:-2] + "xx") is None


def test_expired_token_rejected(monkeypatch):
    token = auth.sign_session("a@b.c")
    # Negative max-age: anything signed in the past is already expired.
    monkeypatch.setattr(config, "SESSION_MAX_AGE_MINUTES", -1)
    assert auth.read_session(token) is None


def test_missing_and_garbage_cookies_rejected():
    assert auth.read_session(None) is None
    assert auth.read_session("") is None
    assert auth.read_session("not-a-token") is None


def test_token_from_other_secret_rejected(monkeypatch):
    token = auth.sign_session("a@b.c")
    monkeypatch.setattr(config, "SESSION_SECRET", "different-secret")
    assert auth.read_session(token) is None


@pytest.mark.parametrize(("given", "expected"), [
    (None, "/ui/"),
    ("", "/ui/"),
    ("/ui/review?status=approved", "/ui/review?status=approved"),
    ("https://evil.example", "/ui/"),
    ("//evil.example/ui/", "/ui/"),
    ("/etc/passwd", "/ui/"),
])
def test_safe_next_blocks_open_redirects(given, expected):
    assert auth.safe_next(given) == expected
