"""Minimal client for the Cove Data Protection JSON-RPC API.

Endpoint and protocol behaviour per the N-able developer portal:
  - POST https://api.backup.management/jsonapi, Content-Type: application/json
  - Login(partner, username, password) returns a "visa" (session token)
  - The visa is valid for 15 minutes
  - Every response carries a fresh visa; chaining them keeps the session alive
  - Method and parameter names are case sensitive
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from .errors import (
    CoveApiError,
    CoveAuthError,
    CoveConfigError,
    CoveTransportError,
)

log = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://api.backup.management/jsonapi"

# N-able documents a 15 minute visa lifetime. Re-authenticate early so a call
# can never be issued against a visa that expires mid-flight.
VISA_LIFETIME_SECONDS = 15 * 60
VISA_REFRESH_MARGIN_SECONDS = 2 * 60

# (connect, read) timeouts. A monitoring job must never hang indefinitely.
DEFAULT_TIMEOUT = (10, 60)


@dataclass
class CoveCredentials:
    partner: str
    username: str
    password: str = field(repr=False)  # keep out of tracebacks and logs
    endpoint: str = DEFAULT_ENDPOINT

    def validate(self) -> None:
        missing = [
            name
            for name in ("partner", "username", "password", "endpoint")
            if not (getattr(self, name) or "").strip()
        ]
        if missing:
            raise CoveConfigError(
                "Missing required credential values: "
                + ", ".join(f"COVE_{n.upper()}" for n in missing)
                + ". Copy .env.example to .env and fill it in."
            )


class CoveClient:
    """Authenticated session against the Cove Management Service.

    Usage:
        client = CoveClient(credentials)
        client.login()
        partner = client.call("GetPartnerInfoById", {"partnerId": client.partner_id})
    """

    def __init__(
        self,
        credentials: CoveCredentials,
        *,
        timeout: tuple[int, int] = DEFAULT_TIMEOUT,
        max_transport_retries: int = 2,
    ) -> None:
        credentials.validate()
        self._credentials = credentials
        self._timeout = timeout
        self._max_transport_retries = max_transport_retries

        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

        self._visa: str | None = None
        self._visa_issued_at: float = 0.0

        self.user_info: dict[str, Any] | None = None
        self.user_id: int | None = None
        self.partner_id: int | None = None
        self.role_id: int | None = None

    # ---------------------------------------------------------------- visa

    @property
    def visa_age_seconds(self) -> float:
        if not self._visa:
            return float("inf")
        return time.monotonic() - self._visa_issued_at

    @property
    def visa_seconds_remaining(self) -> float:
        return max(0.0, VISA_LIFETIME_SECONDS - self.visa_age_seconds)

    def _visa_needs_refresh(self) -> bool:
        return self.visa_seconds_remaining <= VISA_REFRESH_MARGIN_SECONDS

    def _store_visa(self, visa: str | None) -> None:
        """Adopt the visa returned by any response, keeping the chain alive."""
        if visa:
            self._visa = visa
            self._visa_issued_at = time.monotonic()

    # --------------------------------------------------------------- login

    def login(self) -> dict[str, Any]:
        """Authenticate and start a new visa chain. Returns the UserInfo dict."""
        log.info(
            "Authenticating as %s under partner %r",
            self._credentials.username,
            self._credentials.partner,
        )
        payload = {
            "jsonrpc": "2.0",
            "id": "login",
            "method": "Login",
            "params": {
                "partner": self._credentials.partner,
                "username": self._credentials.username,
                "password": self._credentials.password,
            },
        }
        # Login bootstraps the chain, so it carries no visa of its own.
        body = self._post(payload, method="Login", auth_failure=True)

        user_info = self._unwrap(body, method="Login")
        if not isinstance(user_info, dict):
            raise CoveAuthError(
                "Login succeeded but returned no user record.",
                method="Login",
                payload=body,
            )
        if not self._visa:
            raise CoveAuthError(
                "Login returned a user record but no visa; cannot make further calls.",
                method="Login",
                payload=body,
            )

        self.user_info = user_info
        self.user_id = user_info.get("Id")
        self.partner_id = user_info.get("PartnerId")
        self.role_id = user_info.get("RoleId")
        log.info(
            "Authenticated: user id %s, partner id %s, role id %s",
            self.user_id,
            self.partner_id,
            self.role_id,
        )
        return user_info

    def ensure_authenticated(self) -> None:
        if self._visa is None or self._visa_needs_refresh():
            self.login()

    # ---------------------------------------------------------------- call

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Invoke a JSON-RPC method, re-authenticating if the visa is stale.

        Returns the inner result (Cove nests it as result.result).
        """
        self.ensure_authenticated()
        payload = {
            "jsonrpc": "2.0",
            "id": "jsonrpc",
            "visa": self._visa,
            "method": method,
            "params": params or {},
        }
        body = self._post(payload, method=method)
        return self._unwrap(body, method=method)

    # ------------------------------------------------------------ internals

    def _post(
        self,
        payload: dict[str, Any],
        *,
        method: str,
        auth_failure: bool = False,
    ) -> dict[str, Any]:
        """POST a JSON-RPC envelope, retrying only on transport-level faults."""
        endpoint = self._credentials.endpoint
        last_error: Exception | None = None

        for attempt in range(self._max_transport_retries + 1):
            try:
                response = self._session.post(
                    endpoint, json=payload, timeout=self._timeout
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self._max_transport_retries:
                    backoff = 2**attempt
                    log.warning(
                        "Transport error calling %s (attempt %d/%d): %s; retrying in %ss",
                        method,
                        attempt + 1,
                        self._max_transport_retries + 1,
                        exc,
                        backoff,
                    )
                    time.sleep(backoff)
                    continue
                raise CoveTransportError(
                    f"Could not reach {endpoint} calling {method}: {exc}"
                ) from exc

            if response.status_code >= 500 and attempt < self._max_transport_retries:
                backoff = 2**attempt
                log.warning(
                    "HTTP %s from %s calling %s; retrying in %ss",
                    response.status_code,
                    endpoint,
                    method,
                    backoff,
                )
                time.sleep(backoff)
                continue

            if response.status_code != 200:
                raise CoveTransportError(
                    f"HTTP {response.status_code} calling {method}: "
                    f"{response.text[:500]}"
                )

            try:
                body = response.json()
            except ValueError as exc:
                raise CoveTransportError(
                    f"Non-JSON response calling {method}: {response.text[:500]}"
                ) from exc

            # Adopt the new visa before error handling: even a failed call may
            # return one, and dropping it would break the chain.
            if isinstance(body, dict):
                self._store_visa(body.get("visa"))
            self._raise_for_rpc_error(body, method=method, auth_failure=auth_failure)
            return body

        raise CoveTransportError(f"Exhausted retries calling {method}: {last_error}")

    @staticmethod
    def _raise_for_rpc_error(
        body: Any, *, method: str, auth_failure: bool = False
    ) -> None:
        """Surface a JSON-RPC error.

        Cove returns errors with HTTP 200, so the body must be inspected -
        status codes alone would read every failure as a success. Observed
        shape:

            {"error": {"code": -32603, "data": 2100,
                       "message": "Unknown partner/username or bad password"}}

        `code` is the generic JSON-RPC code; `data` carries the Cove-specific
        code, which is the useful one for branching.
        """
        if not isinstance(body, dict):
            raise CoveApiError(
                f"Unexpected response type from {method}: {type(body).__name__}",
                method=method,
                payload=body,
            )

        error = body.get("error")
        if not error:
            return

        if isinstance(error, dict):
            rpc_code = error.get("code")
            cove_code = error.get("data")
            message = error.get("message") or error.get("Message") or str(error)
        else:
            rpc_code = None
            cove_code = None
            message = str(error)

        detail = f"{method} failed: {message}"
        if cove_code is not None:
            detail += f" (cove code {cove_code}, jsonrpc {rpc_code})"

        # 2100 is returned for unknown partner/username or bad password.
        cls = CoveAuthError if (auth_failure or cove_code == 2100) else CoveApiError
        raise cls(
            detail,
            method=method,
            code=cove_code if cove_code is not None else rpc_code,
            payload=body,
        )

    @staticmethod
    def _unwrap(body: dict[str, Any], *, method: str) -> Any:
        """Cove nests payloads as result.result; return the inner value."""
        outer = body.get("result")
        if outer is None:
            raise CoveApiError(
                f"{method} returned no result object.", method=method, payload=body
            )
        if isinstance(outer, dict) and "result" in outer:
            return outer["result"]
        return outer
