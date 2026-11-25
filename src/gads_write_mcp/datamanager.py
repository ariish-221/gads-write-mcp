"""The only module that talks to the Data Manager API, Google's API for conversion imports.

Since 2026-06-15 the Google Ads API refuses UploadClickConversions from projects that had
not imported conversions before, so imports go here instead. Ingestion is asynchronous:
ingest() returns a request id, and the outcome is read later with request_status(), as
counts per reason rather than per row.
"""

from __future__ import annotations

import functools
import re
from typing import Any

import google.auth
import requests
from google.auth.exceptions import DefaultCredentialsError, GoogleAuthError
from google.auth.transport.requests import AuthorizedSession

from .ads import AdsError
from .config import Settings

SCOPE = "https://www.googleapis.com/auth/datamanager"
_BASE = "https://datamanager.googleapis.com/v1"
_TIMEOUT_SECONDS = 60

LOGIN_COMMAND = (
    "gcloud auth application-default login --scopes https://www.googleapis.com/auth/adwords,"
    f"https://www.googleapis.com/auth/cloud-platform,{SCOPE} --client-id-file=YOUR_OAUTH_CLIENT.json"
)


@functools.lru_cache(maxsize=1)
def _session() -> AuthorizedSession:
    try:
        credentials, _ = google.auth.default(scopes=[SCOPE])
    except DefaultCredentialsError as exc:
        raise AdsError(f"No Google credentials found. The user can run: {LOGIN_COMMAND}") from exc
    return AuthorizedSession(credentials)


def ingest(settings: Settings, payload: dict[str, Any], *, validate_only: bool) -> dict[str, Any]:
    """Send events. Returns Google's response: a request id, plus field warnings if any."""
    # Last line of defence: in read-only mode nothing is ever ingested.
    body = {**payload, "validateOnly": validate_only or settings.read_only}
    return _call("POST", "events:ingest", json=body)


def request_status(request_id: str) -> dict[str, Any]:
    return _call("GET", "requestStatus:retrieve", params={"requestId": request_id})


def _call(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = _session().request(method, f"{_BASE}/{path}", timeout=_TIMEOUT_SECONDS, **kwargs)
    except GoogleAuthError as exc:
        raise AdsError(_auth_failure(exc)) from exc
    except requests.RequestException as exc:
        raise AdsError(f"Could not reach the Data Manager API ({type(exc).__name__}). Nothing was sent.") from exc
    if response.ok:
        return response.json() if response.content else {}
    try:
        body = response.json()
    except ValueError:
        body = {}
    raise AdsError(format_failure(response.status_code, body))


def _auth_failure(exc: GoogleAuthError) -> str:
    if "invalid_scope" in str(exc):
        return f"What to do: {_HINTS['ACCESS_TOKEN_SCOPE_INSUFFICIENT']}"
    return f"Google credentials could not be refreshed ({type(exc).__name__}). The user can run: {LOGIN_COMMAND}"


# What to do about errors whose message alone does not say.
_HINTS = {
    "SERVICE_DISABLED": (
        "The Data Manager API is not enabled on the Cloud project these credentials belong to. The user "
        "can enable it at the activation link above, or with: gcloud services enable "
        "datamanager.googleapis.com --project PROJECT_ID. It takes a few minutes to take effect."
    ),
    "ACCESS_TOKEN_SCOPE_INSUFFICIENT": (
        "These credentials were created without the Data Manager scope, which conversion imports "
        f"need. The user can add it by running: {LOGIN_COMMAND}"
    ),
    "UNAUTHENTICATED": f"The Google credentials expired or were revoked. The user can run: {LOGIN_COMMAND}",
    "PERMISSION_DENIED": (
        "The signed-in Google user cannot write conversions to this account. If the account is only "
        "reached through a manager account, the user must set GOOGLE_ADS_LOGIN_CUSTOMER_ID to the "
        "manager id in this server's environment."
    ),
    "RESOURCE_EXHAUSTED": "The Data Manager API quota for the Cloud project is used up. Retry later.",
}

# Google writes event paths as 'events.events[3].event_timestamp'.
_EVENT_INDEX = re.compile(r"(?:events\.)?events\[(\d+)\]\.?")


def format_failure(status_code: int, body: dict[str, Any]) -> str:
    """Google's REST error -> readable lines. Field paths are rewritten to row numbers."""
    error = body.get("error") or {}
    status = error.get("status") or f"HTTP {status_code}"
    lines = [f"The Data Manager API rejected the request ({status}): {error.get('message') or 'no message'}"]
    reasons: list[str] = [status]
    for detail in error.get("details") or []:
        kind = str(detail.get("@type", "")).rsplit(".", 1)[-1]
        if kind == "BadRequest":
            for violation in detail.get("fieldViolations") or []:
                field = _EVENT_INDEX.sub(lambda m: f"row[{m.group(1)}] ", str(violation.get("field", "")))
                reason = f" ({violation['reason']})" if violation.get("reason") else ""
                lines.append(f"- {field.strip()}: {violation.get('description', '')}{reason}")
        elif kind == "ErrorInfo":
            reasons.insert(0, str(detail.get("reason", "")))
            metadata = detail.get("metadata") or {}
            where = ", ".join(metadata[key] for key in ("consumer", "activationUrl") if metadata.get(key))
            lines.append(f"- {detail.get('reason')}" + (f" ({where})" if where else ""))
    hints = list(dict.fromkeys(_HINTS[reason] for reason in reasons if reason in _HINTS))
    return "\n".join([*lines, *(f"What to do: {hint}" for hint in hints[:1])])
