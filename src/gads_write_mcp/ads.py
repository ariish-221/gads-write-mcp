"""The only module that talks to the Google Ads API."""

from __future__ import annotations

import functools
import os
from typing import Any

import google.auth
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.auth.exceptions import DefaultCredentialsError
from google.protobuf import json_format
from google.protobuf.message import Message as ProtoMessage

from .config import Settings

_ADS_SCOPE = "https://www.googleapis.com/auth/adwords"


class AdsError(RuntimeError):
    """An API or credential failure, already formatted for the caller."""


@functools.lru_cache(maxsize=None)
def get_client(login_customer_id: str | None) -> GoogleAdsClient:
    """Client on Application Default Credentials, the same ones google-ads-mcp uses.

    API access is decided by the Cloud project that owns the OAuth client, so a
    developer token is optional; one is passed through if the environment has it.
    """
    try:
        credentials, _ = google.auth.default(scopes=[_ADS_SCOPE])
    except DefaultCredentialsError as exc:
        raise AdsError(
            "No Google credentials found. Run: gcloud auth application-default login "
            f"--scopes {_ADS_SCOPE},https://www.googleapis.com/auth/cloud-platform "
            "--client-id-file=YOUR_OAUTH_CLIENT.json"
        ) from exc
    return GoogleAdsClient(
        credentials=credentials,
        developer_token=os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN") or None,
        login_customer_id=login_customer_id,
        use_proto_plus=False,
    )


def search(client: GoogleAdsClient, customer_id: str, query: str) -> list[dict[str, Any]]:
    service = client.get_service("GoogleAdsService")
    try:
        rows = service.search(customer_id=customer_id, query=query)
        return [json_format.MessageToDict(_pb(row), preserving_proto_field_name=True) for row in rows]
    except GoogleAdsException as exc:
        raise AdsError(format_failure(exc)) from exc


def mutate(
    client: GoogleAdsClient,
    settings: Settings,
    customer_id: str,
    operations: list[Any],
    *,
    validate_only: bool,
) -> list[str]:
    """Send operations atomically. Returns resource names (empty when validating)."""
    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = customer_id
    request.mutate_operations.extend(operations)
    request.partial_failure = False
    # Last line of defence: in read-only mode nothing is ever executed.
    request.validate_only = validate_only or settings.read_only
    try:
        response = client.get_service("GoogleAdsService").mutate(request=request)
    except GoogleAdsException as exc:
        raise AdsError(format_failure(exc)) from exc
    return result_names(response)


def upload_adjustments(
    client: GoogleAdsClient,
    settings: Settings,
    customer_id: str,
    adjustments: list[dict[str, Any]],
    *,
    validate_only: bool,
) -> list[str | None]:
    """Send conversion adjustments. Returns one entry per adjustment: None if Google accepted it, else why not.

    Unlike mutate this is not atomic: the service only works with partial_failure, so
    accepted adjustments go through while rejected ones are reported by index.
    """
    request = client.get_type("UploadConversionAdjustmentsRequest")
    request.customer_id = customer_id
    for data in adjustments:
        json_format.ParseDict(data, request.conversion_adjustments.add())
    request.partial_failure = True
    # Last line of defence: in read-only mode nothing is ever executed.
    request.validate_only = validate_only or settings.read_only
    try:
        response = client.get_service("ConversionAdjustmentUploadService").upload_conversion_adjustments(request=request)
    except GoogleAdsException as exc:
        raise AdsError(format_failure(exc)) from exc
    return partial_failures(client, _pb(response).partial_failure_error, len(adjustments))


def partial_failures(client: GoogleAdsClient, status: Any, count: int) -> list[str | None]:
    """Spread a partial_failure_error over the rows it names. Errors without an index go to every row."""
    outcome: list[str | None] = [None] * count
    for detail in status.details:
        if not detail.type_url.endswith("GoogleAdsFailure"):
            continue
        failure = _pb(client.get_type("GoogleAdsFailure"))
        failure.ParseFromString(detail.value)
        for error in failure.errors:
            line, hint = _describe(error)
            text = f"{line} What to do: {hint}" if hint else line
            indexes = [el.index for el in error.location.field_path_elements if el.HasField("index")][:1]
            for index in indexes or range(count):
                if 0 <= index < count:
                    outcome[index] = f"{outcome[index]}; {text}" if outcome[index] else text
    return outcome


def result_names(response: Any) -> list[str]:
    names = []
    for item in _pb(response).mutate_operation_responses:
        kind = item.WhichOneof("response")
        if kind:
            names.append(getattr(item, kind).resource_name)
    return names


def _pb(message: Any) -> Any:
    """The library hands back proto-plus wrappers in places even with use_proto_plus=False."""
    return message if isinstance(message, ProtoMessage) else type(message).pb(message)


# What to do about errors whose message alone does not say.
_HINTS = {
    "EU_POLITICAL_ADVERTISING_DECLARATION_REQUIRED": (
        "A campaign in this account has no EU political advertising declaration, and Google "
        "blocks every campaign-management change in the account until it has one. Find it with: "
        "SELECT campaign.id, campaign.name FROM campaign WHERE "
        "campaign.missing_eu_political_advertising_declaration = TRUE. The declaration is a legal "
        "statement, so ask the user which value applies, then plan an update of that campaign's "
        "contains_eu_political_advertising."
    ),
    "MISSING_EU_POLITICAL_ADVERTISING_SELF_DECLARATION": (
        "This campaign has no EU political advertising declaration. Ask the user which value "
        "applies, then plan an update of contains_eu_political_advertising first."
    ),
    "CUSTOMER_NOT_ENABLED": "The account is deactivated or not yet enabled; nothing can be changed in it.",
    "DEVELOPER_TOKEN_NOT_APPROVED": (
        "The Cloud project that owns the OAuth client only has Test access. Check "
        "https://console.cloud.google.com/google/ads-apis/overview."
    ),
    "CUSTOMER_NOT_ALLOWLISTED_FOR_THIS_FEATURE": (
        "Google has closed this service to Cloud projects that were not already using it (conversion imports "
        "since 2026-06-15; the Data Manager API replaces them). If this came from a retraction, no API route "
        "is left: the user removes or corrects the conversions in the Google Ads UI."
    ),
    "TOO_RECENT_CONVERSION": "Google only adjusts a conversion once it is at least 24 hours old. Plan the retraction again later.",
    "CONVERSION_NOT_FOUND": (
        "Google has no such conversion: it was never imported, was rejected or discarded (gads_get_upload_status "
        "shows rejections), or is still being processed, which can take up to 24 hours after an upload."
    ),
    "CONVERSION_ALREADY_RETRACTED": "This conversion was retracted before; nothing is left to do for it.",
    "TOO_RECENT_CONVERSION_ACTION": "The conversion action is new. Google needs about 6 hours before it accepts imports or adjustments for it.",
    "RESOURCE_EXHAUSTED": "The project's daily operation quota is used up (Explorer: 2,880 on production). It is a sliding 24 hours.",
}


def format_failure(exc: GoogleAdsException) -> str:
    lines = [f"Google Ads rejected the request (request id {exc.request_id}):"]
    hints: list[str] = []
    for error in _pb(exc.failure).errors:
        line, hint = _describe(error, with_path=True)
        lines.append(f"- {line}")
        if hint and hint not in hints:
            hints.append(hint)
    return "\n".join([*lines, *(f"What to do: {hint}" for hint in hints)])


def _describe(error: Any, with_path: bool = False) -> tuple[str, str | None]:
    """One GoogleAdsError as 'code.REASON: message', plus what to do about it if known."""
    code = error.error_code.WhichOneof("error_code")
    reason = getattr(error.error_code, code) if code else ""
    reason_name = _enum_name(error.error_code, code, reason)
    path = ".".join(
        f"{el.field_name}[{el.index}]" if el.HasField("index") else el.field_name
        for el in error.location.field_path_elements
    )
    at = f" at {path}" if path and with_path else ""
    return f"{code}.{reason_name}{at}: {error.message}", _HINTS.get(reason_name)


def _enum_name(error_code: Any, field: str | None, value: Any) -> str:
    if not field:
        return "UNKNOWN"
    try:
        enum = error_code.DESCRIPTOR.fields_by_name[field].enum_type
        return enum.values_by_number[value].name
    except (KeyError, AttributeError):
        return str(value)
