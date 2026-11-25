"""Offline conversion rows: parsing, hashing, and the requests built from them.

Emails and phone numbers are normalized and SHA-256 hashed here, in memory, as the first
thing that happens to a row. Nothing in this module stores, logs or echoes a raw value:
problems with those fields name the row and the field, never the content.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from .ops import OperationError

MAX_CSV_BYTES = 5_000_000
# Rows shown in a tool result. The plan file always holds every row.
PREVIEW_ROWS = 20

_CLICK_IDS = ("gclid", "gbraid", "wbraid")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CLICK_ID = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9_\-=.]{20,512}$")
_NUMBER = re.compile(r"^\d+(\.\d+)?$")
_SAFE_NAME = re.compile(r"^[A-Za-z][A-Za-z _\-]{0,39}$")


class ConversionRow(BaseModel):
    """One offline conversion. Give a click id, an email or phone, or both: both matches best."""

    model_config = ConfigDict(extra="forbid")

    gclid: str | None = Field(default=None, description="Google click id captured with the lead. At most one of gclid, gbraid, wbraid.")
    gbraid: str | None = Field(default=None, description="Click id for iOS app conversions.")
    wbraid: str | None = Field(default=None, description="Click id for iOS web conversions.")
    email: str | None = Field(default=None, description="The lead's email, raw or already SHA-256 hashed (64 hex chars). The server hashes it before anything is stored.")
    phone: str | None = Field(default=None, description="The lead's phone with country code, e.g. +14165550199, raw or already hashed.")
    conversion_time: str = Field(description="When the conversion happened, with a UTC offset: '2026-09-18 14:30:00-04:00'. Without an offset, pass the tool's 'timezone'.")
    value: float | None = Field(default=None, description="Conversion value in normal units, e.g. 250.00. Omit to use the conversion action's default.")
    currency: str | None = Field(default=None, description="ISO 4217 code, e.g. CAD. Defaults to the tool's 'currency'.")
    order_id: str | None = Field(default=None, description="The CRM's id for this sale or lead. Makes duplicates detectable and the conversion retractable. Never an email or phone.")


# CSV headers and inline keys -> canonical field. Includes Google's own import template headers.
_ALIASES = {
    **{name: name for name in (*_CLICK_IDS, "email", "phone", "hashed_email", "hashed_phone", "conversion_time", "value", "currency", "order_id", "conversion_name")},
    "google_click_id": "gclid", "click_id": "gclid",
    "email_address": "email", "e_mail": "email", "courriel": "email",
    "phone_number": "phone", "mobile": "phone", "mobile_phone": "phone", "telephone": "phone",
    "hashed_email_address": "hashed_email", "hashed_phone_number": "hashed_phone",
    "conversion_date_time": "conversion_time", "conversion_date": "conversion_time", "timestamp": "conversion_time",
    "conversion_value": "value", "conversion_currency": "currency", "currency_code": "currency",
    "transaction_id": "order_id",
}


# --- hashing ------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_email(raw: str) -> str | None:
    """Google's rule: no whitespace, lowercase; gmail.com and googlemail.com also lose a '+suffix' and every dot in the name.

    Checked against Google's Data Manager formatter and its tests (tests/test_offline.py holds the vectors).
    """
    email = re.sub(r"\s+", "", raw).lower()
    if not _EMAIL.match(email):
        return None
    name, _, domain = email.rpartition("@")
    if domain in ("gmail.com", "googlemail.com"):
        name = name.split("+")[0].replace(".", "")
    return f"{name}@{domain}" if name else None


def normalize_phone(raw: str) -> str | None:
    """Google's rule: E.164, a plus sign then digits only, country code included.

    Stricter than Google's formatter on one point, because a number hashed without its country code
    never matches and nothing reports it: a bare 10-digit North American number is refused. Without
    a leading '+', 11 digits or more are needed; a leading 0 is a national prefix, never a country code.
    """
    digits = re.sub(r"\D", "", raw)
    shortest = 7 if raw.strip().startswith("+") else 11
    if digits.startswith("0") or not shortest <= len(digits) <= 15:
        return None
    return f"+{digits}"


_NORMALIZERS = {"email": normalize_email, "phone": normalize_phone}
_PII_HELP = {
    "email": "is not a valid email address",
    "phone": "is not a phone number with its country code (like +14165550199)",
}


def _hash_identifier(kind: str, value: Any) -> str | None:
    """Raw or pre-hashed identifier -> lowercase hex SHA-256, or None if it is unusable."""
    if not isinstance(value, str):
        return None
    if _HASH.match(value.strip().lower()):
        return value.strip().lower()
    normalized = _NORMALIZERS[kind](value)
    return _sha256(normalized) if normalized else None


# --- parsing ------------------------------------------------------------------


def _slug(key: Any) -> str:
    text = unicodedata.normalize("NFKD", str(key)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _safe_name(key: Any) -> str:
    """A header is shown only if it cannot be personal data (a headerless file turns a data row into headers)."""
    return repr(key) if isinstance(key, str) and _SAFE_NAME.match(key) else "(name withheld)"


def read_csv(path: str) -> tuple[list[dict[str, Any]], list[int], list[str]]:
    """Rows of a local CSV, their line numbers, and notes on how its columns were read.

    Only recognized columns are kept, so whatever else the export carries (names, addresses,
    notes) is never held, stored or sent.
    """
    file = Path(path).expanduser()
    if not file.is_file():
        raise OperationError(f"No CSV file at {file}. Pass an absolute path to a file on this machine.")
    if file.stat().st_size > MAX_CSV_BYTES:
        raise OperationError(f"{file.name} is larger than {MAX_CSV_BYTES // 1_000_000} MB. Split the export.")
    try:
        text = file.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise OperationError(f"{file.name} is not UTF-8 text. Re-export it as 'CSV UTF-8'.") from exc

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text, newline=""), dialect=dialect)
    headers = [h for h in (reader.fieldnames or []) if h is not None]
    used = {h: _ALIASES[_slug(h)] for h in headers if _slug(h) in _ALIASES}
    if not set(used.values()) & {*_CLICK_IDS, "email", "phone", "hashed_email", "hashed_phone"} or "conversion_time" not in used.values():
        raise OperationError(
            f"{file.name} needs a header row with a conversion time column and at least one of: gclid "
            "(or 'Google Click ID'), gbraid, wbraid, email, phone. Columns seen: "
            + (", ".join(_safe_name(h) for h in headers) or "none")
        )
    rows, lines = [], []
    for record in reader:
        row = {h: record.get(h) for h in used}
        if any(isinstance(v, str) and v.strip() for v in row.values()):
            rows.append(row)
            lines.append(reader.line_num)
    notes = ["Columns used: " + ", ".join(f"{_safe_name(h)} -> {field}" for h, field in used.items())]
    ignored = [h for h in headers if h not in used]
    if ignored:
        notes.append(f"{len(ignored)} other columns were ignored and never read into the plan: " + ", ".join(_safe_name(h) for h in ignored))
    return rows, lines, notes


def parse_rows(
    raw_rows: Any, *, timezone: str | None, currency: str | None, lines: list[int] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Untrusted rows -> (clean rows holding hashed identifiers only, every problem found).

    All problems are collected, not just the first, so one pass over a bad export fixes it.
    """
    if not isinstance(raw_rows, list) or not raw_rows:
        raise OperationError("'rows' must be a non-empty list of objects.")
    zone = _zone(timezone)
    if currency is not None and not re.fullmatch(r"[A-Za-z]{3}", currency):
        raise OperationError("'currency' must be a 3-letter ISO 4217 code such as CAD.")

    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    for index, raw in enumerate(raw_rows):
        line = {"line": lines[index]} if lines else {}
        before = len(problems)
        row = _parse_row(raw, label(index, line), zone, currency, problems)
        if len(problems) == before:
            rows.append({**row, **line})
    return rows, problems


def label(index: int, row: dict[str, Any]) -> str:
    return f"row[{index}]" + (f" (CSV line {row['line']})" if "line" in row else "")


def _zone(name: str | None) -> ZoneInfo | None:
    if name is None:
        return None
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError) as exc:
        raise OperationError(f"'timezone' {name!r} is not an IANA timezone. Example: America/Toronto.") from exc


def _parse_row(raw: Any, label: str, zone: ZoneInfo | None, currency: str | None, problems: list[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        problems.append(f"{label}: is not an object with named fields.")
        return {}
    fields: dict[str, Any] = {}
    for key, value in raw.items():
        name = _ALIASES.get(_slug(key))
        if name is None:
            problems.append(f"{label}: unknown field {_safe_name(key)}. Allowed: {', '.join(ConversionRow.model_fields)}.")
        elif value is not None and not (isinstance(value, str) and not value.strip()):
            fields.setdefault(name, value)

    row: dict[str, Any] = {}
    # Personal data first: 'row' only ever receives hashes, so nothing built from it can hold a raw value.
    identified = False
    for kind in ("email", "phone"):
        given = [fields.pop(name) for name in (kind, f"hashed_{kind}") if name in fields]
        if given:
            identified = True
            hashed = _hash_identifier(kind, given[0])
            if hashed is None:
                problems.append(f"{label}: {kind} {_PII_HELP[kind]}. The value is withheld from this message.")
            else:
                row[f"hashed_{kind}"] = hashed

    # A shifted column puts personal data where it does not belong. Such values are dropped unseen.
    for name in list(fields):
        text = str(fields[name]).strip()
        if "@" in text or re.fullmatch(r"\+\d{10,15}", text):
            problems.append(f"{label}: {name} looks like an email or phone number. Personal data is only accepted in 'email' and 'phone'. The value is withheld.")
            del fields[name]
        elif name != "value":
            fields[name] = text

    click_ids = [name for name in _CLICK_IDS if name in fields]
    if len(click_ids) > 1:
        problems.append(f"{label}: has {' and '.join(click_ids)}. A conversion carries one click id.")
    for name in click_ids:
        if _CLICK_ID.match(fields[name]):
            row[name] = fields[name]
        else:
            problems.append(f"{label}: {name} does not look like a click id (too short, or characters no click id has). The export may have altered it.")
    if not click_ids and not identified:
        problems.append(f"{label}: needs a click id (gclid, gbraid or wbraid), an email or a phone.")

    moment = _parse_time(fields.get("conversion_time"), zone)
    if isinstance(moment, str):
        problems.append(f"{label}: {moment}")
    else:
        row["conversion_time"] = moment.isoformat(timespec="seconds")
        if len(fields["conversion_time"]) <= 10:
            row["date_only"] = True

    if "value" in fields:
        value = _parse_number(fields["value"])
        if value is None:
            problems.append(f"{label}: value {str(fields['value'])[:40]!r} is not a plain non-negative number like 250.00 (no currency signs or thousands separators).")
        else:
            row["value"] = value
            code = str(fields.get("currency") or currency or "").upper()
            if code and not re.fullmatch(r"[A-Z]{3}", code):
                problems.append(f"{label}: currency {code[:12]!r} is not a 3-letter ISO 4217 code.")
            elif code:
                row["currency"] = code
    if "order_id" in fields:
        if len(fields["order_id"]) > 64:
            problems.append(f"{label}: order_id is longer than 64 characters.")
        row["order_id"] = fields["order_id"]
    if "conversion_name" in fields:
        row["conversion_name"] = fields["conversion_name"]
    return row


def _parse_time(text: str | None, zone: ZoneInfo | None) -> datetime | str:
    if not text:
        return "conversion_time is missing."
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return f"conversion_time {text[:40]!r} is not readable. Use '2026-09-18 14:30:00-04:00'."
    if moment.tzinfo is None:
        if zone is None:
            return f"conversion_time {text[:40]!r} has no UTC offset. Add one, or pass 'timezone' (e.g. America/Toronto) if every row is in that zone."
        moment = moment.replace(tzinfo=zone)
    return moment


def _parse_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and _NUMBER.match(value.strip()):
        number = float(value.strip())
    else:
        return None
    return number if math.isfinite(number) and number >= 0 else None


# --- requests -----------------------------------------------------------------

_CONSENT_FIELDS = {"ad_user_data": "adUserData", "ad_personalization": "adPersonalization"}
_CONSENT_STATUS = {"GRANTED": "CONSENT_GRANTED", "DENIED": "CONSENT_DENIED"}


def ingest_payload(plan: dict[str, Any], login_customer_id: str | None) -> dict[str, Any]:
    """The Data Manager events:ingest body for a stored upload plan."""
    destination: dict[str, Any] = {
        "operatingAccount": {"accountType": "GOOGLE_ADS", "accountId": plan["customer_id"]},
        "productDestinationId": plan["conversion_action"]["id"],
    }
    if login_customer_id:
        destination["loginAccount"] = {"accountType": "GOOGLE_ADS", "accountId": login_customer_id}
    # Google's reference calls eventSource optional; its guide requires it for offline conversions.
    source = plan.get("event_source", "WEB")
    payload: dict[str, Any] = {"destinations": [destination], "events": [_event(row, source) for row in plan["rows"]]}
    if any(key.startswith("hashed_") for row in plan["rows"] for key in row):
        payload["encoding"] = "HEX"
    consent = {_CONSENT_FIELDS[field]: _CONSENT_STATUS[status] for field, status in (plan.get("consent") or {}).items() if status}
    if consent:
        payload["consent"] = consent
    return payload


def _event(row: dict[str, Any], source: str) -> dict[str, Any]:
    event: dict[str, Any] = {"eventTimestamp": row["conversion_time"], "eventSource": source}
    for name in _CLICK_IDS:
        if name in row:
            event["adIdentifiers"] = {name: row[name]}
    identifiers = [{field: row[key]} for key, field in (("hashed_email", "emailAddress"), ("hashed_phone", "phoneNumber")) if key in row]
    if identifiers:
        event["userData"] = {"userIdentifiers": identifiers}
    if "order_id" in row:
        event["transactionId"] = row["order_id"]
    if "value" in row:
        event["conversionValue"] = row["value"]
        if "currency" in row:
            event["currency"] = row["currency"]
    return event


def ads_time(iso: str) -> str:
    """RFC 3339 -> the Google Ads API's 'yyyy-mm-dd hh:mm:ss+hh:mm'."""
    moment = datetime.fromisoformat(iso)
    offset = moment.strftime("%z")
    return moment.strftime("%Y-%m-%d %H:%M:%S") + f"{offset[:3]}:{offset[3:5]}"


def retractable(row: dict[str, Any]) -> bool:
    """Google finds a conversion by its order_id, or by gclid and time, and cannot adjust wbraid conversions."""
    return "wbraid" not in row and ("order_id" in row or "gclid" in row)


def retraction(row: dict[str, Any], conversion_action: str, adjusted_at: str) -> dict[str, Any] | None:
    """A ConversionAdjustment that retracts an uploaded row, or None if Google could not find or adjust it.

    The order_id wins when there is one: Google refuses a gclid pair for a conversion imported with an order_id.
    """
    if not retractable(row):
        return None
    adjustment = {"conversion_action": conversion_action, "adjustment_type": "RETRACTION", "adjustment_date_time": ads_time(adjusted_at)}
    if "order_id" in row:
        return {**adjustment, "order_id": row["order_id"]}
    return {**adjustment, "gclid_date_time_pair": {"gclid": row["gclid"], "conversion_date_time": ads_time(row["conversion_time"])}}


# --- what the human reads -----------------------------------------------------


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, float] = {}
    for row in rows:
        if "value" in row:
            code = row.get("currency", "account currency")
            totals[code] = round(totals.get(code, 0.0) + row["value"], 2)
    times = sorted(row["conversion_time"] for row in rows)
    return {
        "rows": len(rows),
        "total_value": totals,
        "rows_without_value": sum("value" not in row for row in rows),
        "first_conversion": times[0] if times else None,
        "last_conversion": times[-1] if times else None,
        "identifiers": {name: count for name in (*_CLICK_IDS, "hashed_email", "hashed_phone") if (count := sum(name in row for row in rows))},
        "rows_with_order_id": sum("order_id" in row for row in rows),
    }


def preview(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows for a tool result: hashes shortened, so the reader can tell rows apart without carrying them around."""
    shown = []
    for index, row in enumerate(rows[:PREVIEW_ROWS]):
        view = {"row": index, **{k: v for k, v in row.items() if k not in ("date_only", "conversion_name")}}
        for key in ("hashed_email", "hashed_phone"):
            if key in view:
                view[key] = f"sha256:{view[key][:8]}..."
        shown.append(view)
    return shown


# What a rejection reason means for the person fixing the export. Keys are Google's reason
# names without their PROCESSING_ERROR_REASON_ prefix.
_REASON_HELP = {
    "CLICK_NOT_FOUND": "No such click in this account: the id was altered on the way, or belongs to another account.",
    "INVALID_GCLID": "Not a readable gclid: the export probably altered it (case, truncation).",
    "INVALID_GBRAID": "Not a readable gbraid.",
    "INVALID_WBRAID": "Not a readable wbraid.",
    "TOO_RECENT_CLICK": "Google does not know the click yet. Upload these rows again in a day.",
    "EVENT_TOO_OLD": "The conversion is older than Google accepts.",
    "CONVERSION_PRECEDES_CLICK": "The conversion is timed before its click. Check the times and the timezone.",
    "DUPLICATE_GCLID": "Already imported: same click id and conversion time.",
    "DUPLICATE_TRANSACTION_ID": "Already imported: same order_id.",
    "NO_CONSENT": "Dropped because no consent status was sent for these leads.",
    "DENIED_CONSENT": "Dropped because consent was sent as DENIED.",
    "UNKNOWN_CONSENT": "Dropped because the consent status was unknown.",
    "DESTINATION_ACCOUNT_ENHANCED_CONVERSIONS_TERMS_NOT_SIGNED": "The account has not accepted the customer data terms (Google Ads: Goals > Settings).",
    "INVALID_OPERATING_ACCOUNT_FOR_CLICK": "The click belongs to a different account than the one uploaded to.",
    "OPERATING_ACCOUNT_MISMATCH_FOR_AD_IDENTIFIER": "The click belongs to a different account than the one uploaded to.",
    "ONE_PER_CLICK_CONVERSION_ACTION_NOT_PERMITTED_WITH_BRAID": "gbraid and wbraid rows need a conversion action that counts every conversion.",
    "INTERNAL_ERROR": "Google-side error. Upload these rows again later.",
}


def status_view(status: dict[str, Any]) -> dict[str, Any]:
    """requestStatus:retrieve -> what happened to the upload. Google reports counts per reason, never row numbers."""
    destinations = status.get("requestStatusPerDestination") or []
    if not destinations:
        return {"state": "NOT_AVAILABLE_YET", "meaning": "Google has no status for this upload yet. It asks for a 30 minute wait after an upload."}
    item = destinations[0]

    def counts(info: str, prefix: str) -> list[dict[str, Any]]:
        found = []
        for entry in (item.get(info) or {}).get(f"{prefix}Counts") or []:
            reason = re.sub(r"^PROCESSING_(ERROR|WARNING)(_REASON)?_", "", str(entry.get("reason", "")))
            found.append({"reason": reason, "rows": int(entry.get("recordCount") or 0), "meaning": _REASON_HELP.get(reason, "")})
        return found

    rejected = counts("errorInfo", "error")
    return {
        "state": item.get("requestStatus", "REQUEST_STATUS_UNKNOWN"),
        "rows_received": int((item.get("eventsIngestionStatus") or {}).get("recordCount") or 0),
        "rows_rejected": sum(entry["rows"] for entry in rejected),
        "rejected_by_reason": rejected,
        "warnings_by_reason": counts("warningInfo", "warning"),
    }
