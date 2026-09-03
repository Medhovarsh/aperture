"""Signed caller assertions.

A static principals file is fine for a single-tenant deployment where one server
process serves one identity. It stops being fine the moment a gateway fronts many
users: something has to carry "this request is Kim, acting for customer support"
across a process boundary without letting the agent choose the answer.

That is what an assertion is. A short-lived, signed statement minted by a trusted
issuer - an API gateway, an IdP exchange, a session broker - and verified here.

Design decisions worth defending:

* **HMAC-SHA256 over the standard library.** No cryptography dependency, so the
  plane still installs inside a locked-down network. The tradeoff is a shared
  secret between issuer and verifier rather than public-key verification;
  :class:`AssertionVerifier` is the seam where an RS256/JWKS verifier drops in.
* **Short TTL plus single use.** Every assertion carries an expiry and a unique
  ``jti``. The ``jti`` is recorded on first use, so a captured token cannot be
  replayed even inside its validity window.
* **The purpose is inside the signature.** Purpose changes what data an identity
  can reach, so letting the caller pass it alongside an assertion that did not
  cover it would hand back the escalation the signature was meant to prevent.
* **Failures are reason codes, never exceptions.** A malformed token is an
  authorization outcome and belongs in the audit log like any other.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .reasons import Reason

DEFAULT_TTL_SECONDS = 120

#: Reject assertions issued absurdly far in the future rather than trusting a
#: caller's clock. Small enough to be honest skew, large enough not to be brittle.
MAX_CLOCK_SKEW_SECONDS = 30


class NonceStore(Protocol):
    """Records which assertions have been used."""

    def remember_nonce(self, nonce: str, expires_at: datetime) -> bool:
        """Return False when the nonce has been seen before."""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def sign_assertion(
    secret: str,
    principal_id: str,
    purpose: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    issued_at: float | None = None,
) -> str:
    """Mint a signed assertion.

    Intended for the issuing side - a gateway, or a test. The plane itself only
    ever verifies.
    """
    now = issued_at if issued_at is not None else time.time()
    payload = {
        "sub": principal_id,
        "purpose": purpose,
        "iat": int(now),
        "exp": int(now + ttl_seconds),
        "jti": uuid.uuid4().hex,
    }
    body = _b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64encode(signature)}"


@dataclass(frozen=True)
class VerifiedAssertion:
    """A caller assertion that passed every check."""

    principal_id: str
    purpose: str
    expires_at: datetime
    jti: str


@dataclass(frozen=True)
class AssertionFailure:
    """Why an assertion was rejected."""

    reason: Reason
    detail: str = ""


class AssertionVerifier:
    """Verifies signed caller assertions.

    Args:
        secret: Shared signing secret. An empty secret disables verification and
            is refused at construction, because a verifier that accepts everything
            is worse than none at all - it looks like a control.
        nonce_store: Records used assertions so they cannot be replayed. Optional
            only because a stateless verifier is occasionally the right tradeoff;
            when it is absent, replay protection is genuinely off.
    """

    def __init__(self, secret: str, nonce_store: NonceStore | None = None) -> None:
        if not secret or len(secret) < 16:
            raise ValueError(
                "signing secret must be at least 16 characters; refusing to run "
                "with a weak or empty secret"
            )
        self._secret = secret.encode("utf-8")
        self._nonce_store = nonce_store

    @property
    def replay_protected(self) -> bool:
        """True when used assertions are recorded and cannot be presented twice."""
        return self._nonce_store is not None

    def verify(
        self, token: str, now: float | None = None
    ) -> VerifiedAssertion | AssertionFailure:
        """Check an assertion's signature, validity window, and single use."""
        if not token or "." not in token:
            return AssertionFailure(Reason.INVALID_ASSERTION, "malformed token")

        body, _, provided = token.partition(".")
        expected = hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest()
        try:
            signature = _b64decode(provided)
        except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
            return AssertionFailure(Reason.INVALID_ASSERTION, "signature is not valid base64")

        # Constant-time comparison: a timing-variable check on a signature is a
        # forgery oracle.
        if not hmac.compare_digest(expected, signature):
            return AssertionFailure(Reason.INVALID_ASSERTION, "signature does not verify")

        try:
            payload: dict[str, Any] = json.loads(_b64decode(body))
        except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
            return AssertionFailure(Reason.INVALID_ASSERTION, "payload is not valid JSON")

        principal_id = payload.get("sub")
        purpose = payload.get("purpose")
        expires = payload.get("exp")
        issued = payload.get("iat")
        jti = payload.get("jti")
        if not all(isinstance(v, str) for v in (principal_id, purpose, jti)):
            return AssertionFailure(Reason.INVALID_ASSERTION, "missing sub, purpose, or jti")
        if not isinstance(expires, int) or not isinstance(issued, int):
            return AssertionFailure(Reason.INVALID_ASSERTION, "missing iat or exp")

        reference = now if now is not None else time.time()
        if reference >= expires:
            return AssertionFailure(Reason.ASSERTION_EXPIRED, "assertion has expired")
        if issued > reference + MAX_CLOCK_SKEW_SECONDS:
            return AssertionFailure(Reason.ASSERTION_EXPIRED, "assertion is issued in the future")

        expires_at = datetime.fromtimestamp(expires, tz=timezone.utc)
        if self._nonce_store is not None:
            if not self._nonce_store.remember_nonce(str(jti), expires_at):
                return AssertionFailure(Reason.ASSERTION_REPLAYED, "assertion already used")

        return VerifiedAssertion(
            principal_id=str(principal_id),
            purpose=str(purpose),
            expires_at=expires_at,
            jti=str(jti),
        )
