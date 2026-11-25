"""Settings, read once from the environment at startup.

Every limit fails closed: with no allowlist set, plans can be built and
validated against any account, but nothing can be applied.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Removing these is routine and cheap to redo. Campaigns, ad groups, ads and
# budgets cannot be restored once removed, so they are left out: pause instead.
DEFAULT_REMOVABLE = (
    "ad_group_criterion",
    "campaign_criterion",
    "shared_criterion",
    "customer_negative_criterion",
    "campaign_asset",
    "ad_group_asset",
    "customer_asset",
    "campaign_shared_set",
    "campaign_label",
    "ad_group_label",
    "ad_group_ad_label",
)

# How gads_apply_plan gets its human approval:
#   auto    the client's un-skippable permission prompt when the client is known to honour
#           the tool's requiresUserInteraction flag (Claude Code 2.1.199+), else a dialog
#   elicit  always a server-driven dialog (MCP elicitation); fails closed without one
#   client  always the client's own permission prompt
CONFIRM_MODES = ("auto", "elicit", "client")


def _csv(name: str, default: tuple[str, ...] = ()) -> frozenset[str]:
    raw = os.environ.get(name)
    if raw is None:
        return frozenset(default)
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _number(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise SystemExit(f"{name} must be greater than zero, got {raw!r}")
    return value


def clean_customer_id(value: str | int) -> str:
    """'123-456-7890' -> '1234567890'. Raises ValueError on anything else."""
    digits = str(value).replace("-", "").strip()
    if not (digits.isdigit() and len(digits) == 10):
        raise ValueError(
            f"customer_id must be 10 digits (dashes allowed), got {value!r}"
        )
    return digits


@dataclass(frozen=True)
class Settings:
    allowed_customer_ids: frozenset[str]
    read_only: bool
    confirm_mode: str
    max_daily_budget: float
    max_cpc_bid: float
    max_bid_increase_pct: float
    max_ops_per_plan: int
    max_conversions_per_plan: int
    max_conversion_value: float
    removable_resources: frozenset[str]
    blocked_resources: frozenset[str]
    plan_ttl_minutes: int
    state_dir: Path
    login_customer_id: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        confirm_mode = os.environ.get("GADS_CONFIRM", "auto").strip().lower()
        if confirm_mode not in CONFIRM_MODES:
            raise SystemExit(
                f"GADS_CONFIRM must be one of {CONFIRM_MODES}, got {confirm_mode!r}"
            )
        login = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "").strip()
        return cls(
            allowed_customer_ids=frozenset(
                clean_customer_id(cid) for cid in _csv("GADS_ALLOWED_CUSTOMER_IDS")
            ),
            read_only=_flag("GADS_READ_ONLY"),
            confirm_mode=confirm_mode,
            max_daily_budget=_number("GADS_MAX_DAILY_BUDGET", 100.0),
            max_cpc_bid=_number("GADS_MAX_CPC_BID", 10.0),
            max_bid_increase_pct=_number("GADS_MAX_BID_INCREASE_PCT", 50.0),
            max_ops_per_plan=int(_number("GADS_MAX_OPS_PER_PLAN", 50)),
            # One plan is one request, and Google takes at most 2,000 events in one.
            max_conversions_per_plan=min(int(_number("GADS_MAX_CONVERSIONS_PER_PLAN", 500)), 2000),
            max_conversion_value=_number("GADS_MAX_CONVERSION_VALUE", 10_000.0),
            removable_resources=_csv("GADS_ALLOW_REMOVE", DEFAULT_REMOVABLE),
            blocked_resources=_csv("GADS_BLOCKED_RESOURCES"),
            plan_ttl_minutes=int(_number("GADS_PLAN_TTL_MINUTES", 30)),
            state_dir=Path(
                os.environ.get("GADS_STATE_DIR", "~/.google-ads-ai")
            ).expanduser(),
            login_customer_id=clean_customer_id(login) if login else None,
        )

    def describe(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("allowed_customer_ids", "removable_resources", "blocked_resources"):
            data[key] = sorted(data[key])
        data["state_dir"] = str(self.state_dir)
        return data
