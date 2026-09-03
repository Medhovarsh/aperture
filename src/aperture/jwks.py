"""RS256 assertion verification against a JWKS endpoint.

Enterprises already have an identity provider. Okta, Entra, Auth0, Keycloak and
Google all mint RS256 JWTs and publish their public keys at a JWKS URL. This module
verifies those directly, so Aperture can consume the tokens an organization already
issues instead of asking it to adopt a bespoke shared secret.

Why this sits beside the HMAC verifier rather than replacing it:

* HMAC needs no dependencies, so the plane still installs in a network that cannot
  reach PyPI for a native wheel. It stays the default.
* RS256 needs public-key crypto. That arrives through the optional ``idp`` extra,
  and this module raises a clear error rather than silently degrading if it is
  missing. A verifier that quietly stops verifying is worse than one that refuses
  to start.

Verification decisions worth defending:

* **The signing algorithm comes from the key, not the token.** Trusting the token's
  own ``alg`` header is the classic JWT confusion attack: an attacker flips it to
  ``HS256`` and signs with the public key as if it were a shared secret. This
  verifier only ever accepts RS256, and never treats header material as authority.
* **``none`` is rejected outright**, for the same reason.
* **Issuer and audience are checked**, because a valid token from the wrong tenant
  or minted for a different service is not authorization for this one.
* **Keys are cached with a TTL and refetched on unknown ``kid``**, so provider key
  rotation does not cause an outage, but a rotation storm cannot be used to hammer
  the JWKS endpoint either.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .assertions import AssertionFailure, NonceStore, VerifiedAssertion
from .reasons import Reason

#: Refetch keys no more often than this, even when an unknown kid arrives.
MIN_REFETCH_INTERVAL_SECONDS = 30

#: How long a fetched key set stays usable before a scheduled refresh.
DEFAULT_CACHE_TTL_SECONDS = 3600

MAX_CLOCK_SKEW_SECONDS = 30


class JwksUnavailable(RuntimeError):
    """Raised when the ``idp`` extra is missing or the JWKS endpoint cannot be reached."""


def _require_crypto():
    """Import the public-key primitives, or explain exactly what is missing."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization  # noqa: F401
        from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised by the error path only
        raise JwksUnavailable(
            "RS256 verification needs the 'idp' extra: pip install 'aperture-plane[idp]'"
        ) from exc
    return hashes, padding, rsa


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _int_from_b64(value: str) -> int:
    return int.from_bytes(_b64decode(value), "big")


@dataclass(frozen=True)
class JwksConfig:
    """Where tokens come from and what makes one acceptable."""

    jwks_url: str
    issuer: str
    audience: str
    #: Claim carrying the principal id. Providers differ: `sub` is standard, but
    #: many deployments put a stable internal id in a custom claim instead.
    principal_claim: str = "sub"
    #: Claim carrying the declared purpose. Purpose must be inside the signature,
    #: so a caller cannot widen it after the fact.
    purpose_claim: str = "purpose"
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS


class JwksKeyCache:
    """Fetches and caches an issuer's public keys."""

    def __init__(self, url: str, ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS) -> None:
        self.url = url
        self.ttl_seconds = ttl_seconds
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def _fetch(self) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(self.url, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise JwksUnavailable(f"could not fetch JWKS from {self.url}: {exc}") from exc

    @staticmethod
    def _to_public_key(jwk: dict[str, Any]):
        """Turn one RSA JWK into a public key object."""
        _hashes, _padding, rsa = _require_crypto()
        if jwk.get("kty") != "RSA":
            raise JwksUnavailable(f"unsupported key type: {jwk.get('kty')}")
        numbers = rsa.RSAPublicNumbers(
            e=_int_from_b64(jwk["e"]), n=_int_from_b64(jwk["n"])
        )
        return numbers.public_key()

    def get(self, kid: str, allow_refresh: bool = True):
        """Return the public key for a key id, refetching if it is unknown."""
        with self._lock:
            expired = (time.monotonic() - self._fetched_at) > self.ttl_seconds
            missing = kid not in self._keys
            may_refetch = (time.monotonic() - self._fetched_at) > MIN_REFETCH_INTERVAL_SECONDS

            if (expired or (missing and may_refetch)) and allow_refresh:
                document = self._fetch()
                self._keys = {
                    key["kid"]: self._to_public_key(key)
                    for key in document.get("keys", [])
                    if key.get("kty") == "RSA" and "kid" in key
                }
                self._fetched_at = time.monotonic()

            return self._keys.get(kid)

    def prime(self, keys: dict[str, Any]) -> None:
        """Install keys directly. Used by tests and by air-gapped deployments."""
        with self._lock:
            self._keys = dict(keys)
            self._fetched_at = time.monotonic()


class JwksAssertionVerifier:
    """Verifies RS256 JWTs from an enterprise identity provider.

    Presents the same interface as :class:`~aperture.assertions.AssertionVerifier`,
    so the MCP server does not care which one it holds.
    """

    def __init__(
        self,
        config: JwksConfig,
        nonce_store: NonceStore | None = None,
        cache: JwksKeyCache | None = None,
    ) -> None:
        _require_crypto()
        self.config = config
        self._nonce_store = nonce_store
        self.cache = cache or JwksKeyCache(config.jwks_url, config.cache_ttl_seconds)

    @property
    def replay_protected(self) -> bool:
        """True when used assertions are recorded and cannot be presented twice."""
        return self._nonce_store is not None

    def verify(
        self, token: str, now: float | None = None
    ) -> VerifiedAssertion | AssertionFailure:
        """Check signature, issuer, audience, validity window, and single use."""
        hashes, padding, _rsa = _require_crypto()

        parts = token.split(".") if token else []
        if len(parts) != 3:
            return AssertionFailure(Reason.INVALID_ASSERTION, "not a three-part JWT")

        header_segment, payload_segment, signature_segment = parts
        try:
            header = json.loads(_b64decode(header_segment))
            claims = json.loads(_b64decode(payload_segment))
            signature = _b64decode(signature_segment)
        except (ValueError, KeyError):
            return AssertionFailure(Reason.INVALID_ASSERTION, "malformed JWT segments")

        # The algorithm is fixed by policy, never read from the token. Honouring the
        # token's own alg is the JWT confusion attack: flip it to HS256 and sign with
        # the public key as if it were a shared secret.
        if header.get("alg") != "RS256":
            return AssertionFailure(
                Reason.INVALID_ASSERTION, f"unsupported algorithm: {header.get('alg')}"
            )

        kid = header.get("kid")
        if not isinstance(kid, str):
            return AssertionFailure(Reason.INVALID_ASSERTION, "token has no key id")

        try:
            key = self.cache.get(kid)
        except JwksUnavailable as exc:
            return AssertionFailure(Reason.INVALID_ASSERTION, str(exc))
        if key is None:
            return AssertionFailure(Reason.INVALID_ASSERTION, f"unknown key id: {kid}")

        signed_input = f"{header_segment}.{payload_segment}".encode("ascii")
        try:
            key.verify(signature, signed_input, padding.PKCS1v15(), hashes.SHA256())
        except Exception:  # noqa: BLE001 - any verification fault is a rejection
            return AssertionFailure(Reason.INVALID_ASSERTION, "signature does not verify")

        if claims.get("iss") != self.config.issuer:
            return AssertionFailure(
                Reason.INVALID_ASSERTION, f"unexpected issuer: {claims.get('iss')}"
            )

        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if self.config.audience not in audiences:
            return AssertionFailure(
                Reason.INVALID_ASSERTION, "token was not minted for this audience"
            )

        expires = claims.get("exp")
        issued = claims.get("iat", 0)
        if not isinstance(expires, (int, float)):
            return AssertionFailure(Reason.INVALID_ASSERTION, "token has no exp")

        reference = now if now is not None else time.time()
        if reference >= expires:
            return AssertionFailure(Reason.ASSERTION_EXPIRED, "token has expired")
        if isinstance(issued, (int, float)) and issued > reference + MAX_CLOCK_SKEW_SECONDS:
            return AssertionFailure(Reason.ASSERTION_EXPIRED, "token is issued in the future")

        principal_id = claims.get(self.config.principal_claim)
        purpose = claims.get(self.config.purpose_claim)
        if not isinstance(principal_id, str) or not isinstance(purpose, str):
            return AssertionFailure(
                Reason.INVALID_ASSERTION,
                f"token is missing {self.config.principal_claim} or "
                f"{self.config.purpose_claim}",
            )

        expires_at = datetime.fromtimestamp(float(expires), tz=timezone.utc)
        jti = claims.get("jti")
        if self._nonce_store is not None:
            if not isinstance(jti, str):
                # Without a jti there is nothing to record, so replay cannot be
                # prevented. Refuse rather than pretend the protection is active.
                return AssertionFailure(
                    Reason.INVALID_ASSERTION,
                    "replay protection is enabled but the token carries no jti",
                )
            if not self._nonce_store.remember_nonce(jti, expires_at):
                return AssertionFailure(Reason.ASSERTION_REPLAYED, "token already used")

        return VerifiedAssertion(
            principal_id=principal_id,
            purpose=purpose,
            expires_at=expires_at,
            jti=str(jti) if isinstance(jti, str) else "",
        )
