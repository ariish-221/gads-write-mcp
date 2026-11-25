"""Checks that run before anything reaches the API. Pure functions, no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .config import Settings
from .conversions import label, retractable
from .ops import RESOURCE_NAME, Operation, dig, walk

MICROS = 1_000_000

# Nothing new goes live in the same step it is created.
FORCE_PAUSED_ON_CREATE = ("campaign", "ad_group_ad")


@dataclass
class Review:
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def _micros(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _money(micros: int) -> str:
    return f"{micros / MICROS:,.2f}"


def review(customer_id: str, operations: list[Operation], settings: Settings) -> Review:
    """Static checks. May set status=PAUSED on creates (reported as a warning)."""
    result = Review()
    if not operations:
        result.violations.append("Plan has no operations.")
        return result
    if len(operations) > settings.max_ops_per_plan:
        result.violations.append(
            f"{len(operations)} operations exceeds the limit of {settings.max_ops_per_plan} "
            "per plan (GADS_MAX_OPS_PER_PLAN). Split the change into smaller plans."
        )

    for index, op in enumerate(operations):
        where = f"op[{index}] {op.action} {op.resource}"

        if op.resource in settings.blocked_resources:
            result.violations.append(f"{where}: '{op.resource}' is blocked (GADS_BLOCKED_RESOURCES).")

        if op.action == "remove" and op.resource not in settings.removable_resources:
            result.violations.append(
                f"{where}: removal is irreversible in Google Ads and is not enabled for "
                f"'{op.resource}'. Pause it instead, or add it to GADS_ALLOW_REMOVE."
            )

        _check_account_scope(customer_id, op, where, result)
        if op.action == "remove":
            continue

        _check_money(op, where, settings, result)

        if op.action == "create" and op.resource in FORCE_PAUSED_ON_CREATE:
            if op.data.get("status") != "PAUSED":
                op.data["status"] = "PAUSED"
                result.warnings.append(
                    f"{where}: status forced to PAUSED. Enable it with a separate plan after review."
                )
    return result


def _check_account_scope(customer_id: str, op: Operation, where: str, result: Review) -> None:
    """Every resource name in the operation must belong to the plan's account."""
    candidates = [op.resource_name] if op.action == "remove" else [v for _, v in walk(op.data)]
    for value in candidates:
        if not isinstance(value, str) or not value.startswith("customers/"):
            continue
        match = RESOURCE_NAME.match(value)
        if not match:
            result.violations.append(f"{where}: malformed resource name {value!r}.")
        elif match.group(1) != customer_id:
            result.violations.append(
                f"{where}: {value!r} belongs to account {match.group(1)}, "
                f"but this plan targets {customer_id}."
            )


def _check_money(op: Operation, where: str, settings: Settings, result: Review) -> None:
    if op.resource == "campaign_budget":
        daily = _micros(op.data.get("amount_micros"))
        if daily is not None:
            _cap(daily, settings.max_daily_budget, "daily budget", "GADS_MAX_DAILY_BUDGET", where, result)
            if daily < MICROS:
                result.warnings.append(
                    f"{where}: daily budget is {_money(daily)}. amount_micros is in micros "
                    "(50.00 -> 50000000). Check the units."
                )
        total = _micros(op.data.get("total_amount_micros"))
        if total is not None:
            _cap(total, settings.max_daily_budget * 31, "total budget", "GADS_MAX_DAILY_BUDGET x 31", where, result)

    for key, value in walk(op.data):
        if "cpc_bid" in key and key.endswith("_micros"):
            bid = _micros(value)
            if bid is None:
                continue
            _cap(bid, settings.max_cpc_bid, key, "GADS_MAX_CPC_BID", where, result)
            if 0 < bid < 10_000:
                result.warnings.append(
                    f"{where}: {key} is {_money(bid)}. Bids are in micros (1.50 -> 1500000). Check the units."
                )


def _cap(micros: int, limit: float, label: str, setting: str, where: str, result: Review) -> None:
    if micros > limit * MICROS:
        result.violations.append(
            f"{where}: {label} {_money(micros)} exceeds the cap of {limit:,.2f} ({setting})."
        )


def review_changes(
    operations: list[Operation], before: list[dict[str, Any] | None], settings: Settings
) -> Review:
    """Relative checks that need the account's current values."""
    result = Review()
    for index, (op, snapshot) in enumerate(zip(operations, before)):
        if op.action != "update" or not snapshot:
            continue
        for path in op.mask:
            if not path.endswith("cpc_bid_micros"):
                continue
            old, new = _micros(dig(snapshot, path)), _micros(dig(op.data, path))
            if not old or new is None or new <= old:
                continue
            increase = (new - old) / old * 100
            if increase > settings.max_bid_increase_pct:
                result.violations.append(
                    f"op[{index}] update {op.resource}: bid {_money(old)} -> {_money(new)} is "
                    f"+{increase:.0f}%, above the {settings.max_bid_increase_pct:.0f}% limit "
                    "(GADS_MAX_BID_INCREASE_PCT)."
                )
    return result


# --- offline conversions ------------------------------------------------------

# "Import from clicks" in the Google Ads UI. Also the type enhanced conversions for leads use.
UPLOAD_ACTION_TYPE = "UPLOAD_CLICKS"
# Google cannot attribute a conversion to a click older than this, whatever the action's own window.
MAX_CONVERSION_AGE_DAYS = 90
# Google may not know a click yet when its conversion arrives this soon after it.
SETTLING_HOURS = 6


def review_conversion_action(action: dict[str, Any] | None, action_id: str, customer_id: str) -> Review:
    """The conversion action must exist in the plan's account, be live, and accept imported clicks."""
    result = Review()
    if action is None:
        result.violations.append(
            f"No conversion action {action_id} in account {customer_id}. Find the id with google-ads-mcp: SELECT "
            "conversion_action.id, conversion_action.name, conversion_action.type, conversion_action.status FROM conversion_action"
        )
        return result
    name = action.get("name", action_id)
    if action.get("status") != "ENABLED":
        result.violations.append(f"Conversion action '{name}' is {action.get('status')}, not ENABLED. Google drops imports into it.")
    if action.get("type") != UPLOAD_ACTION_TYPE:
        result.violations.append(
            f"Conversion action '{name}' has type {action.get('type')}. Offline conversions can only be imported into "
            f"an {UPLOAD_ACTION_TYPE} action ('Import from clicks' in the Google Ads UI)."
        )
    return result


def review_tracking(tracking: dict[str, Any], customer_id: str, with_user_data: bool) -> Review:
    """Account-level conditions, from customer.conversion_tracking_setting."""
    result = Review()
    owner = tracking.get("google_ads_conversion_customer")
    if owner and owner != f"customers/{customer_id}":
        result.violations.append(
            f"Conversions for account {customer_id} are tracked by {owner} (cross-account conversion tracking), which owns "
            "the conversion actions. Plan the upload against that account id, with a conversion action id from it."
        )
    if with_user_data and not tracking.get("accepted_customer_data_terms"):
        result.violations.append(
            "Rows carry emails or phones, but this account has not accepted Google's customer data terms, so Google would "
            "reject them. The user accepts them in Google Ads under Goals > Settings > Customer data terms."
        )
    if with_user_data and not tracking.get("enhanced_conversions_for_leads_enabled"):
        result.warnings.append(
            "Enhanced conversions for leads is not turned on in this account (Goals > Settings). Until it is, Google "
            "matches rows by click id only and ignores their emails and phones."
        )
    return result


def review_conversions(rows: list[dict[str, Any]], settings: Settings, now: datetime, action: dict[str, Any] | None = None) -> Review:
    """Row checks that need no network. Rows come from conversions.parse_rows; 'now' carries a timezone."""
    result = Review()
    if not rows:
        result.violations.append("Plan has no conversions.")
        return result
    if len(rows) > settings.max_conversions_per_plan:
        result.violations.append(
            f"{len(rows)} conversions exceeds the limit of {settings.max_conversions_per_plan} per plan "
            "(GADS_MAX_CONVERSIONS_PER_PLAN). Split the upload into smaller plans."
        )

    window = int((action or {}).get("click_through_lookback_window_days") or MAX_CONVERSION_AGE_DAYS)
    seen: dict[tuple[Any, ...], int] = {}
    orders: dict[str, int] = {}
    settling: list[str] = []
    for index, row in enumerate(rows):
        where = label(index, row)
        moment = datetime.fromisoformat(row["conversion_time"])
        if moment > now:
            result.violations.append(f"{where}: conversion_time {row['conversion_time']} is in the future. Check the timezone.")
        elif now - moment > timedelta(days=window):
            result.violations.append(
                f"{where}: conversion_time {row['conversion_time']} is more than {window} days ago, past the window in "
                "which Google can still attribute it to a click."
            )
        elif now - moment < timedelta(hours=SETTLING_HOURS):
            settling.append(where)

        value = row.get("value")
        if value is not None and value > settings.max_conversion_value:
            result.violations.append(
                f"{where}: value {value:,.2f} exceeds the cap of {settings.max_conversion_value:,.2f} per conversion "
                "(GADS_MAX_CONVERSION_VALUE). Values are in normal units, not cents or micros."
            )

        identity = next((row[k] for k in ("gclid", "gbraid", "wbraid") if k in row), (row.get("hashed_email"), row.get("hashed_phone")))
        key = (identity, moment.timestamp())
        if key in seen:
            result.violations.append(f"{where}: repeats {label(seen[key], rows[seen[key]])} (same identifier and conversion time). Google counts that as one conversion; drop one.")
        seen.setdefault(key, index)
        order = row.get("order_id")
        if order is not None:
            if order in orders:
                result.violations.append(f"{where}: order_id {order!r} is also on {label(orders[order], rows[orders[order]])}. An order id names one conversion.")
            orders.setdefault(order, index)

        expected = (action or {}).get("name")
        if expected and row.get("conversion_name") and row["conversion_name"].casefold() != expected.casefold():
            result.violations.append(f"{where}: is for conversion '{row['conversion_name']}', but this plan uploads to '{expected}'.")

    without_order = sum("order_id" not in row for row in rows)
    if without_order:
        result.warnings.append(f"{without_order} of {len(rows)} rows lack an order_id: uploading such a row twice is only caught by its click id and time.")
    stuck = sum(not retractable(row) for row in rows)
    if stuck:
        result.warnings.append(
            f"{stuck} of {len(rows)} rows could never be retracted: Google finds a conversion by its order_id, or by "
            "gclid and time, and cannot adjust wbraid conversions at all."
        )
    date_only = sum(bool(row.get("date_only")) for row in rows)
    if date_only:
        result.warnings.append(
            f"Rows with a date without a time: {date_only}. Midnight is assumed, and Google rejects a conversion timed before its click: use the real time if the CRM has it."
        )
    if settling:
        result.warnings.append(
            f"Rows converted less than {SETTLING_HOURS} hours ago: {len(settling)} ({', '.join(settling[:5])}{'...' if len(settling) > 5 else ''}). "
            "Google may not know those clicks yet; if the status report shows them as too recent, upload them again later."
        )
    return result
