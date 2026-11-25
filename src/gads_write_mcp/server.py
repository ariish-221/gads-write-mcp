"""MCP server for guarded Google Ads writes.

Every change is two steps. A gads_plan_* tool checks the change against local
guardrails, reads the current values, and has Google validate it without
executing it. gads_apply_plan executes a stored plan, and only after a human
approves it. Plans come in three kinds (campaign changes, conversion uploads,
conversion retractions) and all of them go through that one gate.
"""

from __future__ import annotations

import functools
import re
from datetime import datetime, timedelta
from typing import Annotated, Any, Callable, Literal

import anyio
from mcp.server.mcpserver import AcceptedElicitation, Context, Elicit, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, SkipValidation

from . import ads, conversions, datamanager, guardrails
from .config import Settings, clean_customer_id
from .guide import NOT_POSSIBLE, RECIPES
from .ops import RESOURCE_NAME, Operation, OperationError, build, dig
from .plans import PlanError, PlanStore, now

INSTRUCTIONS = """\
Guarded write access to Google Ads. Reads live in the separate google-ads-mcp server; use it to find ids.
1. Build a change with a gads_plan_* tool. Nothing is executed: Google only validates it.
2. Show the returned plan to the user, including every from -> to value and every warning.
3. Call gads_apply_plan only after the user approves that specific plan in the conversation. Never in the same turn the plan was built.
Money is in the account's currency. Tools that take amounts accept normal units (50.00); gads_plan_mutate takes micros.
Before building a gads_plan_mutate payload for anything new, read gads_get_payload_guide. It also lists what this server cannot do.
Offline conversions: gads_plan_upload_click_conversions. Prefer csv_path over inline rows, so emails and phone numbers never enter the conversation. An uploaded conversion cannot be deleted, only retracted (gads_plan_rollback), so have the user read the row count, total value and dates. Consent is the user's statement about their leads: ask, never assume it.
Never choose a campaign's EU political advertising declaration yourself: it is the user's legal statement.
"""

mcp = MCPServer("google_ads_write_mcp", instructions=INSTRUCTIONS)

PLAN = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
APPLY = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)
LOCAL_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
REMOTE_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

# Field that names a resource for the human reading the plan.
_LABELS = {
    "campaign": "name",
    "ad_group": "name",
    "campaign_budget": "name",
    "asset_group": "name",
    "bidding_strategy": "name",
    "ad_group_criterion": "keyword.text",
    "campaign_criterion": "keyword.text",
}
_MANUAL_BIDDING = ("MANUAL_CPC", "ENHANCED_CPC")

CustomerId = Annotated[str, Field(description="Google Ads account id, 10 digits, dashes allowed.")]
Note = Annotated[str, Field(description="Why this change is being made. Stored in the audit log.", max_length=500)]


@functools.lru_cache(maxsize=1)
def _settings() -> Settings:
    return Settings.from_env()


@functools.lru_cache(maxsize=1)
def _store() -> PlanStore:
    settings = _settings()
    return PlanStore(settings.state_dir, settings.plan_ttl_minutes)


async def _in_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """The Google Ads client is blocking; keep it off the event loop."""
    try:
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
    except (OperationError, PlanError, ads.AdsError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


# --- pipeline -----------------------------------------------------------------


def _snapshot(client: Any, customer_id: str, op: Operation) -> dict[str, Any] | None:
    """Current values of the fields an operation touches, for the diff and for rollback."""
    if op.action == "create":
        return None
    label = _LABELS.get(op.resource)
    for extra in ([label], []) if label else ([],):
        paths = list(dict.fromkeys([*op.mask, *extra])) or ["resource_name"]
        select = ", ".join(f"{op.resource}.{path}" for path in paths)
        query = f"SELECT {select} FROM {op.resource} WHERE {op.resource}.resource_name = '{op.target}'"
        try:
            rows = ads.search(client, customer_id, query)
        except ads.AdsError:
            continue
        return rows[0].get(op.resource, {}) if rows else None
    return None


def _plan(customer_id: str, operations: list[Operation], note: str, origin: str) -> dict[str, Any]:
    settings, store = _settings(), _store()
    customer_id = clean_customer_id(customer_id)
    raw = [op.model_dump(exclude_none=True) for op in operations]

    review = guardrails.review(customer_id, operations, settings)
    if not review.ok:
        store.audit("plan_blocked", customer_id=customer_id, origin=origin, operations=raw, violations=review.violations)
        raise guardrails_error(review.violations)

    client = ads.get_client(settings.login_customer_id)
    protos = [build(client, op) for op in operations]
    before = [_snapshot(client, customer_id, op) for op in operations]
    warnings = list(review.warnings)
    for index, (op, snapshot) in enumerate(zip(operations, before)):
        if op.action != "create" and snapshot is None:
            warnings.append(f"op[{index}]: could not read current values of {op.target}; no diff or rollback for it.")

    changes = guardrails.review_changes(operations, before, settings)
    if not changes.ok:
        store.audit("plan_blocked", customer_id=customer_id, origin=origin, operations=raw, violations=changes.violations)
        raise guardrails_error(changes.violations)

    try:
        ads.mutate(client, settings, customer_id, protos, validate_only=True)
    except ads.AdsError as exc:
        store.audit("plan_rejected", customer_id=customer_id, origin=origin, operations=raw, error=str(exc))
        raise

    plan = store.create(
        customer_id=customer_id,
        origin=origin,
        note=note,
        # Dump again: review() may have forced PAUSED on creates.
        operations=[op.model_dump(exclude_none=True) for op in operations],
        before=before,
        warnings=warnings,
    )
    store.audit("plan_validated", plan_id=plan["plan_id"], customer_id=customer_id, origin=origin, note=note, operations=plan["operations"], before=before)
    return _present(plan)


def guardrails_error(violations: list[str]) -> OperationError:
    return OperationError("Blocked by guardrails, nothing was sent to Google:\n" + "\n".join(f"- {v}" for v in violations))


def _kind(plan: dict[str, Any]) -> str:
    """Plans written before conversion uploads existed have no kind."""
    return plan.get("kind", "mutate")


def _present(plan: dict[str, Any], brief: bool = False) -> dict[str, Any]:
    if _kind(plan) != "mutate":
        return _present_conversions(plan, brief)
    views = []
    for index, (raw, before) in enumerate(zip(plan["operations"], plan["before"])):
        op = Operation(**raw)
        view: dict[str, Any] = {"index": index, "action": op.action, "resource": op.resource}
        if op.target:
            view["target"] = op.target
        label = dig(before, _LABELS.get(op.resource, ""))
        if label:
            view["label"] = label
        if op.action == "update":
            view["changes"] = {path: {"from": dig(before, path), "to": dig(op.data, path)} for path in op.mask}
        elif op.action == "create":
            view["create"] = op.data
        views.append(view)

    result = {key: plan[key] for key in ("plan_id", "status", "customer_id", "expires_at", "note", "warnings")}
    result["operations"] = views
    if plan["status"] == "pending":
        result["validated_by_google"] = True
        result["next_step"] = (
            "Nothing has changed yet. Show this plan to the user. Call gads_apply_plan "
            f"with plan_id '{plan['plan_id']}' only after they approve it."
        )
    return result


class Approval(BaseModel):
    approve: bool = Field(description="Apply this plan to the live Google Ads account?")


_DIALOG_LINES = 12


def _confirmation_text(plan: dict[str, Any]) -> str:
    lines = [f"Apply {plan['plan_id']} to Google Ads account {plan['customer_id']}?"]
    if _kind(plan) != "mutate":
        return "\n".join([*lines, *_conversion_lines(plan)])
    for item in _present(plan)["operations"]:
        name = item.get("label") or item.get("target") or "(new)"
        detail = ", ".join(f"{p}: {c['from']} -> {c['to']}" for p, c in item.get("changes", {}).items())
        lines.append(f"- {item['action']} {item['resource']} {name} {detail}".rstrip())
    hidden = len(lines) - 1 - _DIALOG_LINES
    if hidden > 0:
        lines = [*lines[: _DIALOG_LINES + 1], f"... and {hidden} more operations"]
    return "\n".join(lines)


def _load_for_apply(plan_id: str) -> tuple[dict[str, Any], list[Operation]]:
    try:
        plan = _store().load_pending(plan_id)
        return plan, _check_can_apply(plan)
    except (PlanError, OperationError) as exc:
        raise ToolError(str(exc)) from exc


# Claude Code 2.1.199+ answers this flag with a permission prompt on every call, in every
# permission mode; allow rules and hooks cannot skip it.
REQUIRES_USER_INTERACTION = {"anthropic/requiresUserInteraction": True}
_MIN_CLAUDE_CODE = (2, 1, 199)


def _client(ctx: Context) -> tuple[str, str]:
    info = getattr(getattr(ctx.session, "client_params", None), "client_info", None)
    return getattr(info, "name", "") or "", getattr(info, "version", "") or ""


def _gate(ctx: Context) -> Literal["client_permission_prompt", "elicitation"]:
    mode = _settings().confirm_mode
    if mode == "auto":
        name, version = _client(ctx)
        numbers = tuple(int(n) for n in re.findall(r"\d+", version)[:3])
        is_claude_code = "claudecode" in re.sub(r"[^a-z]", "", name.lower())
        mode = "client" if is_claude_code and len(numbers) == 3 and numbers >= _MIN_CLAUDE_CODE else "elicit"
    return "client_permission_prompt" if mode == "client" else "elicitation"


def _approval(plan_id: str, ctx: Context) -> Elicit[Approval] | Approval:
    """Human approval the model cannot answer for itself.

    Runs before gads_apply_plan. The SDK turns the Elicit marker into a dialog in the
    client, on whichever protocol revision was negotiated. May run more than once per
    call, so it has no side effects and its question depends only on the stored plan.
    """
    plan, _ = _load_for_apply(plan_id)
    if _gate(ctx) == "client_permission_prompt":
        return Approval(approve=True)
    capabilities = ctx.client_capabilities
    if capabilities is None or capabilities.elicitation is None:
        raise ToolError(
            "This client cannot show a confirmation dialog (MCP elicitation), so the plan was "
            "not applied. To rely on the client's own tool-permission prompt instead, the user "
            "can set GADS_CONFIRM=client in this server's environment."
        )
    return Elicit(_confirmation_text(plan), Approval)


def _check_can_apply(plan: dict[str, Any]) -> list[Operation]:
    settings = _settings()
    customer_id = plan["customer_id"]
    if settings.read_only:
        raise PlanError("Server is in read-only mode (GADS_READ_ONLY=true). Plans validate but cannot be applied.")
    if customer_id not in settings.allowed_customer_ids:
        raise PlanError(
            f"Account {customer_id} is not approved for writes. The user must add it to "
            "GADS_ALLOWED_CUSTOMER_IDS in this server's environment and restart it."
        )
    if _kind(plan) != "mutate":
        _recheck_conversions(plan)
        return []
    operations = [Operation(**raw) for raw in plan["operations"]]
    review = guardrails.review(customer_id, operations, settings)
    if not review.ok:
        raise guardrails_error(review.violations)
    return operations


def _execute(plan: dict[str, Any], operations: list[Operation], confirmed_via: str) -> dict[str, Any]:
    settings, store = _settings(), _store()
    if _kind(plan) != "mutate":
        return _EXECUTORS[_kind(plan)](plan, confirmed_via)
    client = ads.get_client(settings.login_customer_id)
    protos = [build(client, op) for op in operations]
    try:
        names = ads.mutate(client, settings, plan["customer_id"], protos, validate_only=False)
    except ads.AdsError as exc:
        plan.update(status="failed", error=str(exc))
        store.save(plan)
        store.audit("apply_failed", plan_id=plan["plan_id"], customer_id=plan["customer_id"], error=str(exc))
        raise
    plan.update(status="applied", applied_at=now().isoformat(timespec="seconds"), results=names, confirmed_via=confirmed_via)
    store.save(plan)
    store.audit("applied", plan_id=plan["plan_id"], customer_id=plan["customer_id"], confirmed_via=confirmed_via, results=names)
    return {
        "plan_id": plan["plan_id"],
        "status": "applied",
        "customer_id": plan["customer_id"],
        "results": names,
        "rollback": f"To restore the previous values, call gads_plan_rollback with plan_id '{plan['plan_id']}'.",
    }


# --- pipeline: offline conversions ----------------------------------------------
#
# Imports go through the Data Manager API (the Google Ads API refuses them from projects
# that had not imported before 2026-06-15); retractions through the Google Ads API.
# Neither is atomic, and neither can be deleted afterwards.

_UPLOAD, _RETRACTION = "conversion_upload", "conversion_retraction"
# Google Ads Help: adjustments reach Smart Bidding within 7 days of the conversion, and are accepted for 54.
_BIDDING_WINDOW_DAYS = 7
_MAX_PROBLEMS = 25
_ACTION_FIELDS = (
    "id", "name", "status", "type", "click_through_lookback_window_days", "value_settings.default_value",
    "value_settings.default_currency_code", "value_settings.always_use_default_value",
)
_TRACKING_FIELDS = ("accepted_customer_data_terms", "enhanced_conversions_for_leads_enabled", "google_ads_conversion_customer")


def _blocked(customer_id: str, origin: str, problems: list[str], **context: Any) -> OperationError:
    """Audit and build the error. Problems never quote an email or phone, so they are safe to store."""
    _store().audit("plan_blocked", customer_id=customer_id, origin=origin, violations=problems, **context)
    extra = len(problems) - _MAX_PROBLEMS
    return guardrails_error(problems[:_MAX_PROBLEMS] + ([f"... and {extra} more problems."] if extra > 0 else []))


def _conversion_action(client: Any, customer_id: str, action_id: str) -> dict[str, Any] | None:
    select = ", ".join(f"conversion_action.{field}" for field in _ACTION_FIELDS)
    rows = ads.search(client, customer_id, f"SELECT {select} FROM conversion_action WHERE conversion_action.id = {action_id}")
    if not rows:
        return None
    found = rows[0]["conversion_action"]
    settings = found.get("value_settings", {})
    return {
        "id": action_id,
        "resource_name": f"customers/{customer_id}/conversionActions/{action_id}",
        "name": found.get("name", ""),
        "status": found.get("status"),
        "type": dig(found, "type"),
        "click_through_lookback_window_days": found.get("click_through_lookback_window_days"),
        "default_value": settings.get("default_value"),
        "default_currency": settings.get("default_currency_code"),
        "always_use_default_value": bool(settings.get("always_use_default_value")),
    }


def _tracking(client: Any, customer_id: str) -> dict[str, Any]:
    select = ", ".join(f"customer.conversion_tracking_setting.{field}" for field in _TRACKING_FIELDS)
    rows = ads.search(client, customer_id, f"SELECT {select} FROM customer")
    return dig(rows[0], "customer.conversion_tracking_setting") or {} if rows else {}


def _plan_upload(
    customer_id: str, action_id: str, raw_rows: Any, csv_path: str | None, timezone: str | None,
    currency: str | None, consent: dict[str, str | None], event_source: str, note: str,
) -> dict[str, Any]:
    settings, store, origin = _settings(), _store(), "gads_plan_upload_click_conversions"
    if (raw_rows is None) == (csv_path is None):
        raise OperationError("Pass exactly one of 'rows' and 'csv_path'.")
    lines, notes = None, []
    if csv_path is not None:
        raw_rows, lines, notes = conversions.read_csv(csv_path)
    rows, problems = conversions.parse_rows(raw_rows, timezone=timezone, currency=currency, lines=lines)
    if problems:
        raise _blocked(customer_id, origin, problems, rows=len(raw_rows))

    client = ads.get_client(settings.login_customer_id)
    action = _conversion_action(client, customer_id, action_id)
    with_user_data = any(key.startswith("hashed_") for row in rows for key in row)
    reviews = [
        guardrails.review_conversion_action(action, action_id, customer_id),
        guardrails.review_tracking(_tracking(client, customer_id), customer_id, with_user_data) if action else guardrails.Review(),
        guardrails.review_conversions(rows, settings, now(), action),
    ]
    violations = [violation for review in reviews for violation in review.violations]
    if violations:
        raise _blocked(customer_id, origin, violations, rows=len(rows), conversion_action=action_id)

    warnings = [*notes, *(warning for review in reviews for warning in review.warnings)]
    if action["always_use_default_value"]:
        warnings.append(f"'{action['name']}' always uses its default value ({action['default_value']}); the row values will be ignored by Google.")
    if not any(consent.values()):
        warnings.append(
            "No consent status is sent, so the account's default applies (Google Ads: Tools > Data Manager > Consent "
            "settings). Google documents these fields as Digital Markets Act consent and can drop rows of European "
            "leads without it. Ask the user whether their leads consented; never assume GRANTED."
        )
    draft = {"customer_id": customer_id, "conversion_action": action, "rows": rows, "consent": consent, "event_source": event_source}
    try:
        response = datamanager.ingest(settings, conversions.ingest_payload(draft, settings.login_customer_id), validate_only=True)
    except ads.AdsError as exc:
        store.audit("plan_rejected", customer_id=customer_id, origin=origin, rows=len(rows), conversion_action=action_id, error=str(exc))
        raise
    warnings += [f"Google: {w.get('field', '')} {w.get('reason', '')} {w.get('description', '')}".strip() for w in response.get("fieldWarnings", [])]

    plan = store.create(kind=_UPLOAD, origin=origin, note=note, warnings=warnings, summary=conversions.summarize(rows), **draft)
    store.audit("plan_validated", plan_id=plan["plan_id"], kind=_UPLOAD, customer_id=customer_id, origin=origin, note=note, conversion_action=action, summary=plan["summary"])
    return _present(plan)


def _plan_retraction(source: dict[str, Any]) -> dict[str, Any]:
    settings, store, origin = _settings(), _store(), "gads_plan_rollback"
    customer_id, action = source["customer_id"], source["conversion_action"]
    items, skipped = [], []
    for index, row in enumerate(source["rows"]):
        if conversions.retractable(row):
            items.append({"row": index, **{key: row[key] for key in ("order_id", "gclid", "conversion_time") if key in row}})
        else:
            skipped.append(f"{conversions.label(index, row)}: Google cannot find it (no order_id and no gclid) or cannot adjust it (wbraid)")
    if not items:
        raise PlanError(f"Plan {source['plan_id']} has no retractable rows. Not reversible: {skipped}")

    client = ads.get_client(settings.login_customer_id)
    try:
        outcome = ads.upload_adjustments(client, settings, customer_id, _adjustments(items, action), validate_only=True)
    except ads.AdsError as exc:
        store.audit("plan_rejected", customer_id=customer_id, origin=origin, source_plan_id=source["plan_id"], error=str(exc))
        raise
    accepted = [item for item, error in zip(items, outcome) if error is None]
    rejected = [f"row[{item['row']}]: {error}" for item, error in zip(items, outcome) if error is not None]
    if not accepted:
        store.audit("plan_rejected", customer_id=customer_id, origin=origin, source_plan_id=source["plan_id"], error=rejected[:_MAX_PROBLEMS])
        raise OperationError(
            f"Google accepted none of the {len(items)} retractions. It only adjusts conversions that are at least 24 "
            "hours old and fully processed, and gads_get_upload_status shows whether the rows were imported at all.\n"
            + "\n".join(f"- {line}" for line in rejected[:_MAX_PROBLEMS])
        )
    warnings = [f"Google rejected {len(rejected)} of {len(items)} retractions in validation; they are left out of this plan."] if rejected else []
    late = sum(now() - datetime.fromisoformat(item["conversion_time"]) > timedelta(days=_BIDDING_WINDOW_DAYS) for item in accepted)
    if late:
        warnings.append(
            f"{late} of these conversions are more than {_BIDDING_WINDOW_DAYS} days old. Google says Smart Bidding only takes a "
            "retraction into account within that window, so for them this corrects reporting, not bidding."
        )
    plan = store.create(
        kind=_RETRACTION, origin=origin, note=f"Retraction of {source['plan_id']}", warnings=warnings, customer_id=customer_id,
        conversion_action=action, source_plan_id=source["plan_id"], adjustments=accepted, rejected=rejected, not_reversible=skipped,
    )
    store.audit("plan_validated", plan_id=plan["plan_id"], kind=_RETRACTION, customer_id=customer_id, origin=origin, source_plan_id=source["plan_id"], adjustments=accepted, rejected=rejected)
    return _present(plan)


def _adjustments(items: list[dict[str, Any]], action: dict[str, Any]) -> list[dict[str, Any]]:
    adjusted_at = now().isoformat(timespec="seconds")
    return [conversions.retraction(item, action["resource_name"], adjusted_at) for item in items]


def _present_conversions(plan: dict[str, Any], brief: bool) -> dict[str, Any]:
    result = {key: plan[key] for key in ("plan_id", "kind", "status", "customer_id", "expires_at", "note", "warnings")}
    result["conversion_action"] = {key: plan["conversion_action"][key] for key in ("id", "name")}
    if _kind(plan) == _UPLOAD:
        result["summary"] = plan["summary"]
        result["consent"] = plan["consent"]
        if not brief:
            result["rows"] = conversions.preview(plan["rows"])
            result["rows_not_shown"] = max(0, len(plan["rows"]) - conversions.PREVIEW_ROWS)
        checked = (
            f"Google validated the format of all {len(plan['rows'])} rows and the destination. It has not matched them to "
            "clicks yet: that happens after the upload, and gads_get_upload_status reports it."
        )
    else:
        result["source_plan_id"] = plan["source_plan_id"]
        result["retractions"] = len(plan["adjustments"])
        result["rejected_in_validation"] = plan["rejected"][:_MAX_PROBLEMS]
        result["not_reversible"] = plan["not_reversible"][:_MAX_PROBLEMS]
        if not brief:
            result["rows"] = plan["adjustments"][: conversions.PREVIEW_ROWS]
        checked = f"Google accepted {len(plan['adjustments'])} retractions in validation and rejected {len(plan['rejected'])}."
    if plan["status"] == "pending":
        result["validated_by_google"] = True
        result["validation"] = checked
        result["next_step"] = (
            "Nothing has changed yet. Show this plan to the user. Call gads_apply_plan "
            f"with plan_id '{plan['plan_id']}' only after they approve it."
        )
    elif "results" in plan:
        result["results"] = plan["results"]
    return result


def _conversion_lines(plan: dict[str, Any]) -> list[str]:
    name = plan["conversion_action"]["name"]
    if _kind(plan) == _RETRACTION:
        return [f"- retract {len(plan['adjustments'])} conversions from '{name}' (uploaded by {plan['source_plan_id']})", "- A retraction cannot be undone."]
    summary = plan["summary"]
    totals = ", ".join(f"{amount:,.2f} {code}" for code, amount in summary["total_value"].items()) or "the action's default value"
    return [
        f"- upload {summary['rows']} offline conversions to '{name}'",
        f"- total value {totals}; dated {summary['first_conversion']} to {summary['last_conversion']}",
        "- Uploaded conversions cannot be deleted, only retracted.",
    ]


def _recheck_conversions(plan: dict[str, Any]) -> None:
    match = RESOURCE_NAME.match(plan["conversion_action"]["resource_name"])
    if not match or match.group(1) != plan["customer_id"]:
        raise guardrails_error([f"The plan's conversion action does not belong to account {plan['customer_id']}."])
    if _kind(plan) == _UPLOAD:
        review = guardrails.review_conversions(plan["rows"], _settings(), now(), plan["conversion_action"])
        if not review.ok:
            raise guardrails_error(review.violations)


def _finish(plan: dict[str, Any], confirmed_via: str, results: dict[str, Any], error: str | None = None) -> None:
    """Record the outcome. The hashes have done their job either way: undo works from order ids and click ids."""
    store = _store()
    for row in plan.get("rows", []):
        row.pop("hashed_email", None)
        row.pop("hashed_phone", None)
    if error is None:
        plan.update(status="applied", applied_at=now().isoformat(timespec="seconds"), results=results, confirmed_via=confirmed_via)
        store.audit("applied", plan_id=plan["plan_id"], kind=_kind(plan), customer_id=plan["customer_id"], confirmed_via=confirmed_via, results=results)
    else:
        plan.update(status="failed", error=error, results=results)
        store.audit("apply_failed", plan_id=plan["plan_id"], kind=_kind(plan), customer_id=plan["customer_id"], error=error)
    store.save(plan)


def _execute_upload(plan: dict[str, Any], confirmed_via: str) -> dict[str, Any]:
    settings = _settings()
    try:
        response = datamanager.ingest(settings, conversions.ingest_payload(plan, settings.login_customer_id), validate_only=False)
    except ads.AdsError as exc:
        _finish(plan, confirmed_via, {}, error=str(exc))
        raise
    _finish(plan, confirmed_via, {"request_id": response.get("requestId"), "rows_sent": len(plan["rows"])})
    return {
        "plan_id": plan["plan_id"],
        "status": "applied",
        "customer_id": plan["customer_id"],
        "results": plan["results"],
        "next_step": (
            "Google accepted the upload and processes it in the background. Call gads_get_upload_status with plan_id "
            f"'{plan['plan_id']}' in about 30 minutes (processing can take up to 24 hours) to see how many rows were "
            "rejected and why."
        ),
        "rollback": (
            f"Conversions cannot be deleted. To retract these, call gads_plan_rollback with plan_id '{plan['plan_id']}' once they are "
            "at least 24 hours old (Google refuses earlier). Smart Bidding only takes a retraction into account within 7 days of the "
            "conversion, and Google accepts none after 54 days."
        ),
    }


def _execute_retraction(plan: dict[str, Any], confirmed_via: str) -> dict[str, Any]:
    settings = _settings()
    client = ads.get_client(settings.login_customer_id)
    try:
        outcome = ads.upload_adjustments(client, settings, plan["customer_id"], _adjustments(plan["adjustments"], plan["conversion_action"]), validate_only=False)
    except ads.AdsError as exc:
        _finish(plan, confirmed_via, {}, error=str(exc))
        raise
    failed = [f"row[{item['row']}]: {error}" for item, error in zip(plan["adjustments"], outcome) if error is not None]
    results = {"retracted": len(outcome) - len(failed), "failed": failed}
    if len(failed) == len(outcome):
        _finish(plan, confirmed_via, results, error="Google rejected every retraction.")
        raise ads.AdsError("Google rejected every retraction, nothing changed:\n" + "\n".join(f"- {line}" for line in failed[:_MAX_PROBLEMS]))
    _finish(plan, confirmed_via, results)
    return {"plan_id": plan["plan_id"], "status": "applied", "customer_id": plan["customer_id"], "results": results, "rollback": "A retraction cannot be undone. To restore a conversion, upload it again."}


_EXECUTORS = {_UPLOAD: _execute_upload, _RETRACTION: _execute_retraction}


def _upload_status(plan_id: str) -> dict[str, Any]:
    plan = _store().load(plan_id)
    if _kind(plan) != _UPLOAD or plan["status"] != "applied":
        raise PlanError(f"Plan {plan_id} is not an applied conversion upload (it is a {plan['status']} {_kind(plan)} plan).")
    status = datamanager.request_status(plan["results"]["request_id"])
    return {"plan_id": plan_id, "rows_sent": plan["results"]["rows_sent"], **conversions.status_view(status)}


# --- tools: generic -----------------------------------------------------------


@mcp.tool(name="gads_plan_mutate", title="Plan any Google Ads change", annotations=PLAN)
async def gads_plan_mutate(
    customer_id: CustomerId,
    operations: Annotated[list[Operation], Field(description="Changes to apply atomically, in order.", min_length=1)],
    note: Note = "",
) -> dict[str, Any]:
    """Plan any create/update/remove on any resource GoogleAdsService.Mutate supports. Executes nothing.

    Prefer the specific gads_plan_* tools when one fits; use this for everything else
    (campaigns, ad groups, sitelinks and other assets, targeting, network settings, shared
    negative lists, editing an ad's text, Performance Max). Call gads_get_payload_guide first
    for a checked payload: several fields are required or renamed in ways the API reference
    does not make obvious. New campaigns and ads are always created PAUSED.

    Example operation: {"resource": "campaign", "action": "update",
      "data": {"resource_name": "customers/1234567890/campaigns/555", "status": "PAUSED"}}

    Returns the validated plan: plan_id, expires_at, per-operation from -> to values, warnings.
    Errors name the failing operation index and field, and whether the block came from
    local guardrails or from Google's validation.
    """
    return await _in_thread(_plan, customer_id, operations, note, "gads_plan_mutate")


@mcp.tool(name="gads_apply_plan", title="Apply an approved plan", annotations=APPLY, meta=REQUIRES_USER_INTERACTION)
async def gads_apply_plan(
    plan_id: Annotated[str, Field(description="Id returned by a gads_plan_* tool, e.g. 'plan_1a2b3c4d'.")],
    approval: Annotated[ElicitationResult[Approval], Resolve(_approval)],
    ctx: Context,
) -> dict[str, Any]:
    """Execute a validated plan against the live account. This is the only tool that changes anything.

    Call it only after the user has seen the plan and approved it in the conversation.
    The user's client then asks them to confirm; the model cannot answer for them.
    Refuses when: the plan is expired or already used, the account is not in
    GADS_ALLOWED_CUSTOMER_IDS, the server is read-only, or the user declines.
    All operations in a plan succeed or fail together. Do not retry a declined plan.

    Returns status 'applied' with the affected resource names.
    """
    # Checked again: the confirmation may have been open for a while.
    plan, operations = _load_for_apply(plan_id)
    if not (isinstance(approval, AcceptedElicitation) and approval.data.approve):
        _store().audit("apply_declined", plan_id=plan_id, customer_id=plan["customer_id"])
        raise ToolError(
            f"{plan_id} was declined and nothing was changed. If the user never saw a dialog, their "
            "client declined on its own (a known problem in some Claude clients): they can set "
            "GADS_CONFIRM=client in this server's environment to use the permission prompt instead."
        )
    return await _in_thread(_execute, plan, operations, _gate(ctx))


@mcp.tool(name="gads_plan_rollback", title="Plan the reversal of an applied plan", annotations=PLAN)
async def gads_plan_rollback(
    plan_id: Annotated[str, Field(description="Id of a plan whose status is 'applied'.")],
) -> dict[str, Any]:
    """Plan restoring the values an applied plan overwrote. Executes nothing; apply it like any plan.

    Only updates can be reversed. Creates and removes are listed under 'not_reversible':
    pause what was created, re-add what was removed.

    For an applied conversion upload this plans a retraction of its rows, which removes them from
    reporting and bidding. Rows are found by order_id, or by gclid and conversion time; rows with
    neither, and wbraid rows, are listed under 'not_reversible'. Google validates every retraction
    and only accepts conversions between 24 hours and 54 days old; Smart Bidding only takes one
    into account within 7 days of the conversion. A retraction cannot be undone.
    """
    return await _in_thread(_plan_rollback, plan_id)


def _plan_rollback(plan_id: str) -> dict[str, Any]:
    plan = _store().load(plan_id)
    if plan["status"] != "applied":
        raise PlanError(f"Plan {plan_id} is {plan['status']}, not applied. There is nothing to roll back.")
    if _kind(plan) == "conversion_retraction":
        raise PlanError(f"Plan {plan_id} is a retraction. Google cannot undo one: upload those conversions again instead.")
    if _kind(plan) == "conversion_upload":
        return _plan_retraction(plan)
    inverse, skipped = [], []
    for index, (raw, before) in enumerate(zip(plan["operations"], plan["before"])):
        op = Operation(**raw)
        if op.action != "update" or not before:
            skipped.append(f"op[{index}] {op.action} {op.resource} {op.target or ''}".rstrip())
            continue
        data: dict[str, Any] = {"resource_name": op.target}
        for path in op.mask:
            value = dig(before, path)
            if value is not None:
                _assign(data, path, value)
        inverse.append(Operation(resource=op.resource, action="update", data=data, update_mask=op.mask))
    if not inverse:
        raise PlanError(f"Plan {plan_id} has no reversible operations. Not reversible: {skipped}")
    result = _plan(plan["customer_id"], inverse, f"Rollback of {plan_id}", "gads_plan_rollback")
    result["not_reversible"] = skipped
    return result


def _assign(data: dict[str, Any], path: str, value: Any) -> None:
    *parents, leaf = path.split(".")
    for key in parents:
        data = data.setdefault(key, {})
    data[leaf] = value


# --- tools: common changes ----------------------------------------------------

_STATUS_TARGETS = {
    "campaign": ("campaign", "campaigns", r"\d+"),
    "ad_group": ("ad_group", "adGroups", r"\d+"),
    "ad": ("ad_group_ad", "adGroupAds", r"\d+~\d+"),
    "keyword": ("ad_group_criterion", "adGroupCriteria", r"\d+~\d+"),
}


@mcp.tool(name="gads_plan_set_status", title="Plan pausing or enabling", annotations=PLAN)
async def gads_plan_set_status(
    customer_id: CustomerId,
    entity_type: Literal["campaign", "ad_group", "ad", "keyword"],
    ids: Annotated[
        list[str],
        Field(description="Campaign or ad group ids ('555'). For ad and keyword: 'adGroupId~adId' or 'adGroupId~criterionId' ('777~999').", min_length=1),
    ],
    status: Literal["ENABLED", "PAUSED"],
    note: Note = "",
) -> dict[str, Any]:
    """Plan pausing or enabling campaigns, ad groups, ads or keywords. Executes nothing.

    Returns the validated plan with each entity's name and its current -> new status.
    """
    cid = _clean(customer_id)
    resource, collection, pattern = _STATUS_TARGETS[entity_type]
    operations = []
    for entity_id in ids:
        if not re.fullmatch(pattern, entity_id):
            expected = "'adGroupId~id'" if "~" in pattern else "a numeric id"
            raise ToolError(f"{entity_type} id {entity_id!r} is not valid; expected {expected}.")
        name = f"customers/{cid}/{collection}/{entity_id}"
        operations.append(Operation(resource=resource, action="update", data={"resource_name": name, "status": status}))
    return await _in_thread(_plan, cid, operations, note, "gads_plan_set_status")


@mcp.tool(name="gads_plan_update_budget", title="Plan a daily budget change", annotations=PLAN)
async def gads_plan_update_budget(
    customer_id: CustomerId,
    campaign_id: Annotated[str, Field(description="Campaign id, digits only.", pattern=r"^\d+$")],
    daily_budget: Annotated[float, Field(description="New daily budget in the account's currency, e.g. 75.00.", gt=0)],
    note: Note = "",
) -> dict[str, Any]:
    """Plan a new daily budget for a campaign. Executes nothing.

    Blocked above GADS_MAX_DAILY_BUDGET. Warns when the budget is shared, because the
    change then affects every campaign on it. Returns the plan with current -> new amount in micros.
    """
    return await _in_thread(_plan_budget, _clean(customer_id), campaign_id, daily_budget, note)


def _plan_budget(customer_id: str, campaign_id: str, daily_budget: float, note: str) -> dict[str, Any]:
    client = ads.get_client(_settings().login_customer_id)
    rows = ads.search(
        client,
        customer_id,
        "SELECT campaign.name, campaign_budget.resource_name, campaign_budget.reference_count "
        f"FROM campaign WHERE campaign.id = {campaign_id}",
    )
    if not rows:
        raise OperationError(f"No campaign {campaign_id} in account {customer_id}.")
    budget = rows[0]["campaign_budget"]
    # Budgets must be a multiple of the currency's minimum unit (0.01).
    micros = round(daily_budget * 100) * 10_000
    operation = Operation(resource="campaign_budget", action="update", data={"resource_name": budget["resource_name"], "amount_micros": micros})
    result = _plan(customer_id, [operation], note, "gads_plan_update_budget")
    shared_by = int(budget.get("reference_count", 1))
    if shared_by > 1:
        result["warnings"].append(f"This budget is shared by {shared_by} campaigns. The change affects all of them.")
    return result


class Keyword(BaseModel):
    text: str = Field(description="Keyword text without match-type punctuation.", min_length=1, max_length=80)
    match_type: Literal["EXACT", "PHRASE", "BROAD"]
    cpc_bid: float | None = Field(default=None, description="Optional max CPC in the account's currency, e.g. 1.50.", gt=0)


class NegativeKeyword(BaseModel):
    text: str = Field(description="Negative keyword text.", min_length=1, max_length=80)
    match_type: Literal["EXACT", "PHRASE", "BROAD"]


@mcp.tool(name="gads_plan_add_keywords", title="Plan adding keywords", annotations=PLAN)
async def gads_plan_add_keywords(
    customer_id: CustomerId,
    ad_group_id: Annotated[str, Field(description="Ad group id, digits only.", pattern=r"^\d+$")],
    keywords: Annotated[list[Keyword], Field(min_length=1)],
    allow_broad_with_manual_bidding: Annotated[
        bool, Field(description="Set true only if the user explicitly wants broad match in a manual-CPC campaign.")
    ] = False,
    note: Note = "",
) -> dict[str, Any]:
    """Plan adding positive keywords to an ad group. Executes nothing.

    Broad match in a manual-bidding campaign is blocked unless explicitly allowed:
    without Smart Bidding it tends to buy irrelevant queries and drag CTR down.
    """
    return await _in_thread(_plan_keywords, _clean(customer_id), ad_group_id, keywords, allow_broad_with_manual_bidding, note)


def _plan_keywords(customer_id: str, ad_group_id: str, keywords: list[Keyword], allow_broad: bool, note: str) -> dict[str, Any]:
    if not allow_broad and any(k.match_type == "BROAD" for k in keywords):
        client = ads.get_client(_settings().login_customer_id)
        rows = ads.search(client, customer_id, f"SELECT campaign.name, campaign.bidding_strategy_type FROM ad_group WHERE ad_group.id = {ad_group_id}")
        if not rows:
            raise OperationError(f"No ad group {ad_group_id} in account {customer_id}.")
        campaign = rows[0]["campaign"]
        if campaign.get("bidding_strategy_type") in _MANUAL_BIDDING:
            raise OperationError(
                f"Campaign '{campaign.get('name')}' uses {campaign['bidding_strategy_type']}. Broad match without "
                "Smart Bidding is blocked. Use PHRASE or EXACT, or pass allow_broad_with_manual_bidding=true "
                "if the user explicitly asked for it."
            )
    ad_group = f"customers/{customer_id}/adGroups/{ad_group_id}"
    operations = []
    for keyword in keywords:
        data: dict[str, Any] = {"ad_group": ad_group, "status": "ENABLED", "keyword": {"text": keyword.text, "match_type": keyword.match_type}}
        if keyword.cpc_bid is not None:
            data["cpc_bid_micros"] = round(keyword.cpc_bid * 100) * 10_000
        operations.append(Operation(resource="ad_group_criterion", action="create", data=data))
    return _plan(customer_id, operations, note, "gads_plan_add_keywords")


@mcp.tool(name="gads_plan_add_negative_keywords", title="Plan adding negative keywords", annotations=PLAN)
async def gads_plan_add_negative_keywords(
    customer_id: CustomerId,
    level: Literal["campaign", "ad_group"],
    parent_id: Annotated[str, Field(description="Campaign id or ad group id, matching 'level'.", pattern=r"^\d+$")],
    keywords: Annotated[list[NegativeKeyword], Field(min_length=1)],
    note: Note = "",
) -> dict[str, Any]:
    """Plan adding negative keywords at campaign or ad group level. Executes nothing."""
    cid = _clean(customer_id)
    if level == "campaign":
        resource, parent = "campaign_criterion", {"campaign": f"customers/{cid}/campaigns/{parent_id}"}
    else:
        resource, parent = "ad_group_criterion", {"ad_group": f"customers/{cid}/adGroups/{parent_id}"}
    operations = [
        Operation(resource=resource, action="create", data={**parent, "negative": True, "keyword": {"text": k.text, "match_type": k.match_type}})
        for k in keywords
    ]
    return await _in_thread(_plan, cid, operations, note, "gads_plan_add_negative_keywords")


class KeywordBid(BaseModel):
    ad_group_id: str = Field(pattern=r"^\d+$")
    criterion_id: str = Field(pattern=r"^\d+$")
    cpc_bid: float = Field(description="New max CPC in the account's currency, e.g. 2.25.", gt=0)


@mcp.tool(name="gads_plan_update_keyword_bids", title="Plan keyword bid changes", annotations=PLAN)
async def gads_plan_update_keyword_bids(
    customer_id: CustomerId,
    bids: Annotated[list[KeywordBid], Field(min_length=1)],
    note: Note = "",
) -> dict[str, Any]:
    """Plan new max CPC bids for keywords. Executes nothing.

    Blocked above GADS_MAX_CPC_BID, and when an increase exceeds GADS_MAX_BID_INCREASE_PCT
    of the current bid. Decreases are not limited.
    """
    cid = _clean(customer_id)
    operations = [
        Operation(
            resource="ad_group_criterion",
            action="update",
            data={"resource_name": f"customers/{cid}/adGroupCriteria/{b.ad_group_id}~{b.criterion_id}", "cpc_bid_micros": round(b.cpc_bid * 100) * 10_000},
        )
        for b in bids
    ]
    return await _in_thread(_plan, cid, operations, note, "gads_plan_update_keyword_bids")


@mcp.tool(name="gads_plan_create_rsa", title="Plan a responsive search ad", annotations=PLAN)
async def gads_plan_create_rsa(
    customer_id: CustomerId,
    ad_group_id: Annotated[str, Field(description="Ad group id, digits only.", pattern=r"^\d+$")],
    headlines: Annotated[list[str], Field(description="3 to 15 headlines, 30 characters max, no exclamation points.", min_length=3, max_length=15)],
    descriptions: Annotated[list[str], Field(description="2 to 4 descriptions, 90 characters max.", min_length=2, max_length=4)],
    final_url: Annotated[str, Field(description="Landing page URL, https://...", pattern=r"^https?://")],
    path1: Annotated[str, Field(description="Optional display path, 15 characters max.", max_length=15)] = "",
    path2: Annotated[str, Field(description="Optional second display path, 15 characters max. Needs path1.", max_length=15)] = "",
    note: Note = "",
) -> dict[str, Any]:
    """Plan a new responsive search ad. It is always created PAUSED. Executes nothing.

    Google's policy check runs during validation, so disapproval-level problems surface here.
    """
    problems = [f"headline {i + 1} is {len(h)} chars (max 30): {h!r}" for i, h in enumerate(headlines) if len(h) > 30]
    problems += [f"headline {i + 1} has an exclamation point: {h!r}" for i, h in enumerate(headlines) if "!" in h]
    problems += [f"description {i + 1} is {len(d)} chars (max 90): {d!r}" for i, d in enumerate(descriptions) if len(d) > 90]
    if len(set(headlines)) < len(headlines):
        problems.append("headlines must be unique")
    if path2 and not path1:
        problems.append("path2 needs path1")
    if problems:
        raise ToolError("Fix the ad copy:\n" + "\n".join(f"- {p}" for p in problems))

    cid = _clean(customer_id)
    rsa: dict[str, Any] = {"headlines": [{"text": h} for h in headlines], "descriptions": [{"text": d} for d in descriptions]}
    if path1:
        rsa["path1"] = path1
    if path2:
        rsa["path2"] = path2
    operation = Operation(
        resource="ad_group_ad",
        action="create",
        data={"ad_group": f"customers/{cid}/adGroups/{ad_group_id}", "status": "PAUSED", "ad": {"final_urls": [final_url], "responsive_search_ad": rsa}},
    )
    return await _in_thread(_plan, cid, [operation], note, "gads_plan_create_rsa")


# --- tools: offline conversions -----------------------------------------------

ConsentStatus = Literal["GRANTED", "DENIED"]


@mcp.tool(name="gads_plan_upload_click_conversions", title="Plan an offline conversion upload", annotations=PLAN)
async def gads_plan_upload_click_conversions(
    customer_id: CustomerId,
    conversion_action_id: Annotated[str, Field(description="Id of an ENABLED conversion action of type UPLOAD_CLICKS ('Import from clicks').", pattern=r"^\d+$")],
    # Not validated by the SDK: its error messages quote the input, and these rows hold emails and phones.
    rows: Annotated[SkipValidation[list[conversions.ConversionRow] | None], Field(description="Conversions, inline. Use csv_path instead whenever the data is in a file.")] = None,
    csv_path: Annotated[
        str | None,
        Field(description="Absolute path to a CSV export on this machine. Needs a header row; Google's import template headers work ('Google Click ID', 'Conversion Time', 'Conversion Value', 'Conversion Currency', 'Email', 'Phone Number', 'Order ID'). Other columns are ignored."),
    ] = None,
    timezone: Annotated[str | None, Field(description="IANA timezone for conversion times that carry no UTC offset, e.g. America/Toronto. Ask the user; do not guess.")] = None,
    currency: Annotated[str | None, Field(description="ISO 4217 code for rows that carry a value but no currency, e.g. CAD.")] = None,
    ad_user_data_consent: Annotated[ConsentStatus | None, Field(description="Whether these leads consented to their data being sent to Google for advertising. The user's statement: ask, never assume.")] = None,
    ad_personalization_consent: Annotated[ConsentStatus | None, Field(description="Whether these leads consented to ad personalization. The user's statement: ask, never assume.")] = None,
    event_source: Annotated[
        Literal["WEB", "PHONE", "IN_STORE", "MESSAGE", "APP", "OTHER"],
        Field(description="Where the conversions happened. WEB is what Google's own examples use for imported leads; Google rejects a source that does not fit the conversion action."),
    ] = "WEB",
    note: Note = "",
) -> dict[str, Any]:
    """Plan uploading offline conversions (CRM sales, qualified leads) for one account and one conversion action. Executes nothing.

    Each row needs a conversion time and a click id (gclid, gbraid or wbraid), an email or phone, or both:
    a click id plus an email matches best (enhanced conversions for leads). Emails and phones are
    normalized and SHA-256 hashed in memory before anything is stored; pass a csv_path and they never
    enter this conversation either.

    Blocked locally, by row number: future or too-old times, values above GADS_MAX_CONVERSION_VALUE,
    repeated rows or order ids, more rows than GADS_MAX_CONVERSIONS_PER_PLAN, personal data outside the
    email and phone fields, and a conversion action that is missing, not ENABLED or not an import action.

    Returns the validated plan: row count, total value, date range, identifier mix, warnings, and the
    first rows. Google validates the format only; whether each row matches a click is known after the
    upload, from gads_get_upload_status. Uploaded conversions cannot be deleted, only retracted.
    """
    consent = {"ad_user_data": ad_user_data_consent, "ad_personalization": ad_personalization_consent}
    return await _in_thread(_plan_upload, _clean(customer_id), conversion_action_id, rows, csv_path, timezone, currency, consent, event_source, note)


@mcp.tool(name="gads_get_upload_status", title="Check an applied conversion upload", annotations=REMOTE_READ)
async def gads_get_upload_status(
    plan_id: Annotated[str, Field(description="Id of an applied conversion upload plan.")],
) -> dict[str, Any]:
    """Ask Google how it processed an applied conversion upload. Changes nothing.

    Returns the processing state and how many rows were rejected for each reason (click not found,
    click too recent, duplicate, no consent...). Google reports counts per reason, not row numbers.
    Google asks for a 30 minute wait before the first check; processing can take up to 24 hours,
    and the status is kept for about 50 days. 'PROCESSING' means ask again later.
    """
    return await _in_thread(_upload_status, plan_id)


# --- tools: local state -------------------------------------------------------


@mcp.tool(name="gads_list_plans", title="List recent plans", annotations=LOCAL_READ)
async def gads_list_plans(
    limit: Annotated[int, Field(description="How many plans to return, newest first.", ge=1, le=50)] = 10,
    status: Annotated[Literal["pending", "applied", "failed", "expired"] | None, Field(description="Only plans with this status.")] = None,
) -> dict[str, Any]:
    """List recent plans from local state, newest first. Does not call Google."""
    plans = [p for p in _store().recent(50) if status is None or p["status"] == status][:limit]
    return {"count": len(plans), "plans": [_present(p, brief=True) for p in plans]}


@mcp.tool(name="gads_get_guardrails", title="Show active guardrails", annotations=LOCAL_READ)
async def gads_get_guardrails(ctx: Context) -> dict[str, Any]:
    """Show the active limits: which accounts may be written to, budget and bid caps, and how
    gads_apply_plan gets its human approval in this client.

    All of it is set through environment variables on this server and can only be changed by the user.
    """
    name, version = _client(ctx)
    return {
        **_settings().describe(),
        "client": {"name": name, "version": version},
        "apply_gate": _gate(ctx),
        "audit_log": str(_store().audit_path),
    }


Topic = Literal[
    "search_campaign", "sitelinks_callouts_snippets", "location_language", "network_settings",
    "shared_negative_list", "rsa_text_update", "image_asset", "performance_max",
]


@mcp.tool(name="gads_get_payload_guide", title="Checked payloads for gads_plan_mutate", annotations=LOCAL_READ)
async def gads_get_payload_guide(
    topic: Annotated[Topic | None, Field(description="Omit to list the topics and what this server cannot do.")] = None,
) -> dict[str, Any]:
    """Return operations for gads_plan_mutate that are known to parse, with the rules Google enforces.

    Replace the placeholder account id 1234567890 and the example ids and text. Does not call Google.
    """
    if topic is None:
        return {"topics": {name: recipe["summary"] for name, recipe in RECIPES.items()}, "not_possible_with_this_server": NOT_POSSIBLE}
    return {"topic": topic, **RECIPES[topic]}


def _clean(customer_id: str) -> str:
    try:
        return clean_customer_id(customer_id)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def main() -> None:
    _settings()
    _store()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
