"""Config classmethod tests — pure logic, no I/O."""
from __future__ import annotations

from app.config import config

ALLOWED = ("business@renegadeinsurance.info", "aayush.gupta@renegade-insurance.com")


def test_sender_allowed_matches_the_allowlist(monkeypatch):
    # Class attr, not instance: sender_allowed reads cls.* (see conftest).
    monkeypatch.setattr(type(config), "SENDER_ALLOWED_ADDRESSES", ALLOWED)
    assert config.sender_allowed("business@renegadeinsurance.info")
    assert config.sender_allowed("aayush.gupta@renegade-insurance.com")
    assert config.sender_allowed("Business@RenegadeInsurance.INFO")   # case-insensitive
    assert not config.sender_allowed("someone@gmail.com")
    # exact address, not just the domain — another address on an allowed
    # domain is still blocked
    assert not config.sender_allowed("random@renegadeinsurance.info")
    assert not config.sender_allowed("")
    assert not config.sender_allowed(None)


def test_empty_allowlist_is_unrestricted(monkeypatch):
    monkeypatch.setattr(type(config), "SENDER_ALLOWED_ADDRESSES", ())
    assert config.sender_allowed("anyone@anywhere.com")
    assert config.sender_allowed(None)
