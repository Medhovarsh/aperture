"""Signed caller assertions."""

from __future__ import annotations

import base64
import json
import time

import pytest

from aperture.assertions import (
    AssertionFailure,
    AssertionVerifier,
    VerifiedAssertion,
    sign_assertion,
)
from aperture.reasons import Reason

SECRET = "test-signing-secret-long-enough"
OTHER_SECRET = "a-different-secret-entirely-ok"


class MemoryNonceStore:
    """Minimal nonce store for tests."""

    def __init__(self) -> None:
        self.seen: set[str] = set()

    def remember_nonce(self, nonce: str, expires_at) -> bool:
        if nonce in self.seen:
            return False
        self.seen.add(nonce)
        return True


def test_round_trip() -> None:
    verifier = AssertionVerifier(SECRET)
    result = verifier.verify(sign_assertion(SECRET, "u_kim", "customer_support"))
    assert isinstance(result, VerifiedAssertion)
    assert result.principal_id == "u_kim"
    assert result.purpose == "customer_support"


def test_weak_secret_is_refused_at_construction() -> None:
    """A verifier that accepts everything is worse than none: it looks like a control."""
    with pytest.raises(ValueError, match="at least 16"):
        AssertionVerifier("short")
    with pytest.raises(ValueError):
        AssertionVerifier("")


def test_wrong_secret_does_not_verify() -> None:
    result = AssertionVerifier(OTHER_SECRET).verify(
        sign_assertion(SECRET, "u_kim", "customer_support")
    )
    assert isinstance(result, AssertionFailure)
    assert result.reason is Reason.INVALID_ASSERTION


def test_tampered_payload_is_rejected() -> None:
    """Editing the identity inside the token must break the signature."""
    token = sign_assertion(SECRET, "u_kim", "customer_support")
    body, _, signature = token.partition(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["sub"] = "u_dana"
    forged_body = (
        base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode()
    )
    result = AssertionVerifier(SECRET).verify(f"{forged_body}.{signature}")
    assert isinstance(result, AssertionFailure)
    assert result.reason is Reason.INVALID_ASSERTION


def test_expired_assertion_is_rejected() -> None:
    token = sign_assertion(SECRET, "u_kim", "customer_support", ttl_seconds=1)
    result = AssertionVerifier(SECRET).verify(token, now=time.time() + 30)
    assert isinstance(result, AssertionFailure)
    assert result.reason is Reason.ASSERTION_EXPIRED


def test_assertion_from_the_future_is_rejected() -> None:
    """A caller's clock is not evidence."""
    token = sign_assertion(SECRET, "u_kim", "customer_support", issued_at=time.time() + 3600)
    result = AssertionVerifier(SECRET).verify(token)
    assert isinstance(result, AssertionFailure)
    assert result.reason is Reason.ASSERTION_EXPIRED


def test_assertion_is_single_use() -> None:
    verifier = AssertionVerifier(SECRET, nonce_store=MemoryNonceStore())
    token = sign_assertion(SECRET, "u_kim", "customer_support")
    assert isinstance(verifier.verify(token), VerifiedAssertion)
    replayed = verifier.verify(token)
    assert isinstance(replayed, AssertionFailure)
    assert replayed.reason is Reason.ASSERTION_REPLAYED


def test_replay_protection_is_reported_honestly() -> None:
    assert AssertionVerifier(SECRET).replay_protected is False
    assert AssertionVerifier(SECRET, nonce_store=MemoryNonceStore()).replay_protected is True


@pytest.mark.parametrize(
    "token", ["", "not-a-token", "onlyonepart", "a.b.c", "!!!.???"]
)
def test_malformed_tokens_are_refusals_not_crashes(token: str) -> None:
    result = AssertionVerifier(SECRET).verify(token)
    assert isinstance(result, AssertionFailure)


def test_nonce_store_backed_by_sqlite_persists_across_verifiers(workspace) -> None:
    """Replay protection must survive process restarts, so it lives in the store."""
    store = workspace.action_store
    token = sign_assertion(SECRET, "u_kim", "customer_support")

    assert isinstance(AssertionVerifier(SECRET, nonce_store=store).verify(token), VerifiedAssertion)

    fresh_verifier = AssertionVerifier(SECRET, nonce_store=workspace.action_store)
    result = fresh_verifier.verify(token)
    assert isinstance(result, AssertionFailure)
    assert result.reason is Reason.ASSERTION_REPLAYED
