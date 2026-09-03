"""RS256 assertion verification against an identity provider.

Tokens are minted here with a throwaway key pair, exactly as Okta or Entra would
mint them, and verified through the public JWK. The attacks that matter for JWT
verification are the ones this file spends most of its lines on.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

cryptography = pytest.importorskip(
    "cryptography", reason="RS256 verification needs the [idp] extra"
)

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from aperture.assertions import AssertionFailure, VerifiedAssertion  # noqa: E402
from aperture.jwks import JwksAssertionVerifier, JwksConfig, JwksKeyCache  # noqa: E402
from aperture.reasons import Reason  # noqa: E402

ISSUER = "https://acme.okta.example"
AUDIENCE = "aperture"
KID = "test-key-1"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.fixture(scope="module")
def keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def verifier(keypair):
    config = JwksConfig(jwks_url="https://unused.example/jwks", issuer=ISSUER, audience=AUDIENCE)
    cache = JwksKeyCache(config.jwks_url)
    cache.prime({KID: keypair.public_key()})
    return JwksAssertionVerifier(config, cache=cache)


def mint(
    keypair,
    *,
    principal: str = "u_kim",
    purpose: str = "customer_support",
    issuer: str = ISSUER,
    audience=AUDIENCE,
    ttl: int = 300,
    alg: str = "RS256",
    kid: str = KID,
    jti: str | None = "jti-1",
    now: float | None = None,
    sign_with=None,
) -> str:
    """Mint a JWT the way a provider would."""
    issued = int(now if now is not None else time.time())
    header = {"alg": alg, "kid": kid, "typ": "JWT"}
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": principal,
        "purpose": purpose,
        "iat": issued,
        "exp": issued + ttl,
    }
    if jti is not None:
        claims["jti"] = jti

    signing_input = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}"
    signer = sign_with or keypair
    signature = signer.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{b64(signature)}"


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #


def test_valid_provider_token_is_accepted(verifier, keypair) -> None:
    result = verifier.verify(mint(keypair))
    assert isinstance(result, VerifiedAssertion)
    assert result.principal_id == "u_kim"
    assert result.purpose == "customer_support"


def test_key_is_resolved_by_kid(verifier, keypair) -> None:
    result = verifier.verify(mint(keypair, kid="unknown-key"))
    assert isinstance(result, AssertionFailure)
    assert "unknown key id" in result.detail


# --------------------------------------------------------------------------- #
# the attacks
# --------------------------------------------------------------------------- #


def test_algorithm_confusion_is_rejected(verifier, keypair) -> None:
    """The classic JWT attack: claim HS256 and sign with the public key as a secret.

    The verifier fixes the algorithm by policy and never reads it from the token,
    so this fails before any signature check.
    """
    import hashlib
    import hmac

    public_pem = keypair.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    header = {"alg": "HS256", "kid": KID, "typ": "JWT"}
    claims = {
        "iss": ISSUER, "aud": AUDIENCE, "sub": "u_dana", "purpose": "hr_support",
        "iat": int(time.time()), "exp": int(time.time()) + 300, "jti": "forged",
    }
    signing_input = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}"
    forged = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()

    result = verifier.verify(f"{signing_input}.{b64(forged)}")
    assert isinstance(result, AssertionFailure)
    assert "unsupported algorithm" in result.detail


def test_alg_none_is_rejected(verifier) -> None:
    header = {"alg": "none", "kid": KID}
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "u_dana", "purpose": "hr_support",
              "exp": int(time.time()) + 300}
    token = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}."
    result = verifier.verify(token)
    assert isinstance(result, AssertionFailure)


def test_token_signed_by_a_different_key_is_rejected(verifier) -> None:
    """A token from another tenant's provider must not authorize anything here."""
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    result = verifier.verify(mint(attacker, sign_with=attacker))
    assert isinstance(result, AssertionFailure)
    assert "signature does not verify" in result.detail


def test_tampered_claims_break_the_signature(verifier, keypair) -> None:
    token = mint(keypair)
    header_segment, payload_segment, signature_segment = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload_segment + "=="))
    claims["sub"] = "u_dana"
    forged_payload = b64(json.dumps(claims).encode())
    result = verifier.verify(f"{header_segment}.{forged_payload}.{signature_segment}")
    assert isinstance(result, AssertionFailure)


def test_wrong_issuer_is_rejected(verifier, keypair) -> None:
    result = verifier.verify(mint(keypair, issuer="https://evil.example"))
    assert isinstance(result, AssertionFailure)
    assert "unexpected issuer" in result.detail


def test_wrong_audience_is_rejected(verifier, keypair) -> None:
    """A valid token minted for another service is not authorization for this one."""
    result = verifier.verify(mint(keypair, audience="some-other-service"))
    assert isinstance(result, AssertionFailure)
    assert "audience" in result.detail


def test_audience_list_is_honoured(verifier, keypair) -> None:
    result = verifier.verify(mint(keypair, audience=["other", AUDIENCE]))
    assert isinstance(result, VerifiedAssertion)


def test_expired_token_is_rejected(verifier, keypair) -> None:
    result = verifier.verify(mint(keypair, ttl=1), now=time.time() + 60)
    assert isinstance(result, AssertionFailure)
    assert result.reason is Reason.ASSERTION_EXPIRED


def test_future_dated_token_is_rejected(verifier, keypair) -> None:
    result = verifier.verify(mint(keypair, now=time.time() + 3600))
    assert isinstance(result, AssertionFailure)
    assert result.reason is Reason.ASSERTION_EXPIRED


def test_missing_purpose_claim_is_rejected(verifier, keypair) -> None:
    """Purpose must be inside the signature, so a token without one is unusable."""
    issued = int(time.time())
    header = {"alg": "RS256", "kid": KID}
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "u_kim", "iat": issued, "exp": issued + 300}
    signing_input = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}"
    signature = keypair.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    result = verifier.verify(f"{signing_input}.{b64(signature)}")
    assert isinstance(result, AssertionFailure)
    assert "purpose" in result.detail


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #


def test_replay_is_blocked_when_a_nonce_store_is_configured(keypair, workspace) -> None:
    config = JwksConfig(jwks_url="https://unused.example/jwks", issuer=ISSUER, audience=AUDIENCE)
    cache = JwksKeyCache(config.jwks_url)
    cache.prime({KID: keypair.public_key()})
    verifier = JwksAssertionVerifier(config, nonce_store=workspace.action_store, cache=cache)

    token = mint(keypair, jti="single-use")
    assert isinstance(verifier.verify(token), VerifiedAssertion)
    replayed = verifier.verify(token)
    assert isinstance(replayed, AssertionFailure)
    assert replayed.reason is Reason.ASSERTION_REPLAYED


def test_token_without_jti_is_refused_when_replay_protection_is_on(keypair, workspace) -> None:
    """Refuse rather than pretend the protection is active."""
    config = JwksConfig(jwks_url="https://unused.example/jwks", issuer=ISSUER, audience=AUDIENCE)
    cache = JwksKeyCache(config.jwks_url)
    cache.prime({KID: keypair.public_key()})
    verifier = JwksAssertionVerifier(config, nonce_store=workspace.action_store, cache=cache)

    result = verifier.verify(mint(keypair, jti=None))
    assert isinstance(result, AssertionFailure)
    assert "no jti" in result.detail


def test_replay_protection_is_reported_honestly(verifier, keypair, workspace) -> None:
    assert verifier.replay_protected is False


# --------------------------------------------------------------------------- #
# key cache
# --------------------------------------------------------------------------- #


def test_jwks_document_is_parsed_into_keys(keypair, monkeypatch: pytest.MonkeyPatch) -> None:
    numbers = keypair.public_key().public_numbers()
    document = {
        "keys": [
            {
                "kty": "RSA",
                "kid": KID,
                "n": b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                "e": b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
            }
        ]
    }
    cache = JwksKeyCache("https://unused.example/jwks")
    monkeypatch.setattr(cache, "_fetch", lambda: document)
    assert cache.get(KID) is not None


def test_unreachable_jwks_is_a_refusal_not_a_crash(keypair) -> None:
    config = JwksConfig(
        jwks_url="http://127.0.0.1:9/jwks", issuer=ISSUER, audience=AUDIENCE
    )
    verifier = JwksAssertionVerifier(config)
    result = verifier.verify(mint(keypair))
    assert isinstance(result, AssertionFailure)
    assert result.reason is Reason.INVALID_ASSERTION
