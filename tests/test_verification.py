"""AI verification logic — no network. Prompt shape, verdict parsing, the
conservative mapping, and the provider gate."""
from __future__ import annotations

import pytest

from app import verification
from app.config import config
from app.providers.base import ProviderError


class FakeTarget:
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.email = kw.get("email", "jane@doe.example")
        self.first_name = kw.get("first_name", "Jane")
        self.last_name = kw.get("last_name", "Doe")
        self.company = kw.get("company", "Doe Insurance")
        self.title = kw.get("title", "Agency Owner")
        self.linkedin_url = kw.get("linkedin_url", "https://www.linkedin.com/in/jane-doe")
        self.profile_data = kw.get("profile_data", {"headline": "Owner at Doe Insurance"})


class FakeVerifier:
    def __init__(self, response):
        self.response = response
        self.calls: list = []

    async def complete_json(self, system, user):
        self.calls.append((system, user))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_prompt_carries_identity_and_profile():
    system, user = verification.build_verify_prompt(FakeTarget())
    assert "verify" in system.lower()
    assert "Jane Doe" in user
    assert "Doe Insurance" in user
    assert "jane@doe.example" in user
    assert "Owner at Doe Insurance" in user  # profile_data serialised in


def test_prompt_truncates_huge_profile(monkeypatch):
    # Patch the instance (not type(config)): build_verify_prompt reads
    # config.DRAFT_PROFILE_CHAR_LIMIT, and a sibling test leaves an instance
    # attribute that would shadow a class-level patch.
    monkeypatch.setattr(config, "DRAFT_PROFILE_CHAR_LIMIT", 50)
    _, user = verification.build_verify_prompt(FakeTarget(profile_data={"x": "y" * 500}))
    assert "y" * 500 not in user


@pytest.mark.parametrize("raw,verdict,is_match", [
    ({"verdict": "right_person", "confidence": 0.9, "reason": "same co"}, "right_person", True),
    ({"verdict": "wrong_person", "confidence": 0.8, "reason": "diff co"}, "wrong_person", False),
    ({"verdict": "unsure", "confidence": 0.4, "reason": "thin"}, "unsure", False),
    ({"verdict": "banana", "confidence": 1, "reason": ""}, "unsure", False),   # unknown -> unsure
    ({"reason": "no verdict key"}, "unsure", False),                            # missing -> unsure
    ({"verdict": "right_person", "confidence": "high"}, "right_person", True),  # bad conf -> 0.0
])
def test_parse_verdict_maps_conservatively(raw, verdict, is_match):
    v = verification.parse_verdict(raw)
    assert v.verdict == verdict
    assert v.is_match is is_match
    assert 0.0 <= v.confidence <= 1.0


@pytest.mark.asyncio
async def test_verify_match_returns_verdict():
    verifier = FakeVerifier(
        {"verdict": "wrong_person", "confidence": 0.85, "reason": "different company"})
    v = await verification.verify_match(FakeTarget(), verifier)
    assert v.verdict == "wrong_person" and not v.is_match
    assert "different company" in v.reason
    assert v.as_reason().startswith("AI: wrong_person")


@pytest.mark.asyncio
async def test_verify_match_propagates_provider_error():
    with pytest.raises(ProviderError):
        await verification.verify_match(FakeTarget(), FakeVerifier(ProviderError("n8n down")))


def test_build_verifier_gate(monkeypatch):
    monkeypatch.setattr(type(config), "DRAFT_PROVIDER", "n8n")
    monkeypatch.setattr(type(config), "N8N_LLM_URL", "https://n8n.example/webhook/llm")
    from app.providers.n8n_llm import N8nDrafter
    assert isinstance(verification.build_verifier(), N8nDrafter)

    monkeypatch.setattr(type(config), "N8N_LLM_URL", "")
    assert verification.build_verifier() is None  # n8n selected but no URL

    monkeypatch.setattr(type(config), "DRAFT_PROVIDER", "anthropic")
    assert verification.build_verifier() is None  # no complete_json provider
