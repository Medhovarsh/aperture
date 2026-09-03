"""HTTP executor: the shape every real integration takes.

The SQLite executors prove the gateway works. This one proves the abstraction
survives contact with a real system, because almost every enterprise action -
issue a refund in Stripe, close a ticket in Zendesk, disable an account in Entra -
is ultimately an authenticated HTTP call to somebody else's API.

It is deliberately built on the standard library. Adding `requests` or `httpx` to
a security control's dependency tree buys convenience and costs supply chain.

Three endpoints, matching the gateway's three phases:

* ``estimate_url``  - dry run. Must not change anything, and the contract says so.
* ``execute_url``   - perform, returning a compensation record when reversible.
* ``compensate_url``- undo, using only what execute returned.

Safety properties enforced here rather than trusted to the remote service:

* **HTTPS is required** unless the host is explicitly localhost, so an action's
  credentials never cross a network in the clear.
* **Redirects are not followed.** A 302 from an action endpoint is how an SSRF
  turns one allowlisted URL into an arbitrary one.
* **Credentials come from the environment**, named in the catalog but never
  written there, so a governance file can be committed to git.
* **Timeouts are mandatory.** A hung action endpoint must not hold a claim open.
* **A non-2xx response is a failure**, and the gateway's boundary turns it into a
  refusal with a reason code rather than a crash.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from ..types import Sensitivity  # noqa: F401  (kept for symmetry with other executors)
from .executors import Executor, ExecutorError
from .types import ActionSpec, BlastRadius

DEFAULT_TIMEOUT_SECONDS = 10
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuses redirects.

    Following one would let a compromised or misconfigured endpoint point the
    plane's credentialed request at an arbitrary host.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise ExecutorError(f"action endpoint attempted a redirect to {newurl}")


class HttpExecutor(Executor):
    """Calls an external HTTP service to estimate, perform, and undo an action.

    Catalog configuration::

        config:
          estimate_url: https://ops.internal.example/refunds/estimate
          execute_url: https://ops.internal.example/refunds
          compensate_url: https://ops.internal.example/refunds/reverse
          auth_header: Authorization
          auth_env: OPS_API_TOKEN
          timeout_seconds: 10
    """

    name = "http"
    reversible = True  # Per-action; the catalog's own claim is what the gateway checks.

    # -- request plumbing ------------------------------------------------- #

    @staticmethod
    def _validate_url(url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ExecutorError(f"action endpoint must be http(s): {url}")
        host = (parsed.hostname or "").lower()
        if parsed.scheme == "http" and host not in LOCAL_HOSTS:
            raise ExecutorError(
                f"refusing to send action credentials over plaintext http to {host}"
            )
        return url

    def _headers(self, spec: ActionSpec) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        auth_env = spec.config.get("auth_env")
        if auth_env:
            token = os.environ.get(str(auth_env))
            if not token:
                raise ExecutorError(
                    f"action {spec.id} needs {auth_env} in the environment"
                )
            headers[str(spec.config.get("auth_header", "Authorization"))] = token
        return headers

    def _post(self, spec: ActionSpec, url_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = spec.config.get(url_key)
        if not url:
            raise ExecutorError(f"action {spec.id} has no '{url_key}' configured")

        request = urllib.request.Request(
            self._validate_url(str(url)),
            data=json.dumps(payload, default=str).encode("utf-8"),
            headers=self._headers(spec),
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirects)
        timeout = float(spec.config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))

        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise ExecutorError(f"{spec.id}: endpoint returned {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ExecutorError(f"{spec.id}: endpoint unreachable: {exc}") from exc

        if not body.strip():
            return {}
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ExecutorError(f"{spec.id}: endpoint returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise ExecutorError(f"{spec.id}: endpoint must return a JSON object")
        return parsed

    # -- Executor interface ----------------------------------------------- #

    def estimate(self, spec: ActionSpec, arguments: dict[str, Any]) -> BlastRadius:
        """Ask the remote service what this action would touch.

        The remote service reports impact; it does not report permission. Policy is
        evaluated here regardless of what comes back, so a compromised endpoint can
        overstate its blast radius but never talk its way past a limit.
        """
        payload = self._post(
            spec, "estimate_url", {"action": spec.id, "arguments": arguments, "dry_run": True}
        )
        summary = payload.get("summary")
        if not isinstance(summary, str) or not summary:
            raise ExecutorError(f"{spec.id}: estimate response has no summary")

        return BlastRadius(
            summary=summary,
            affected=int(payload.get("affected", 0) or 0),
            amount=float(payload.get("amount", 0) or 0),
            currency=str(payload.get("currency", "USD")),
            external_recipients=payload.get("external_recipients") or (),
            reversible=spec.reversible,
            details=payload.get("details") or {},
        )

    def execute(
        self, spec: ActionSpec, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Perform the action remotely and capture how to undo it."""
        payload = self._post(spec, "execute_url", {"action": spec.id, "arguments": arguments})
        result = payload.get("result", payload)
        compensation = payload.get("compensation")

        if spec.reversible and compensation is None:
            # The catalog promised this could be undone and the service returned
            # nothing to undo it with. Surfacing that as a failure is the only
            # honest option: the alternative is an execution that silently cannot
            # be rolled back despite the approval screen saying it can.
            raise ExecutorError(
                f"{spec.id} is declared reversible but the endpoint returned no "
                f"compensation record"
            )
        return dict(result) if isinstance(result, dict) else {"response": result}, compensation

    def compensate(self, spec: ActionSpec, compensation: dict[str, Any]) -> dict[str, Any]:
        """Undo a previous execution remotely."""
        payload = self._post(
            spec, "compensate_url", {"action": spec.id, "compensation": compensation}
        )
        result = payload.get("result", payload)
        return dict(result) if isinstance(result, dict) else {"response": result}
