"""Pending plans on disk, plus an append-only audit log of everything attempted."""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_PLAN_ID = re.compile(r"^plan_[0-9a-f]{8}$")


class PlanError(LookupError):
    """The plan cannot be used. The message says why."""


def now() -> datetime:
    return datetime.now(timezone.utc)


class PlanStore:
    def __init__(self, state_dir: Path, ttl_minutes: int) -> None:
        self.plans_dir = state_dir / "plans"
        self.audit_path = state_dir / "audit.jsonl"
        self.ttl = timedelta(minutes=ttl_minutes)
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(state_dir, 0o700)

    def create(self, **fields: Any) -> dict[str, Any]:
        created = now()
        plan = {
            "plan_id": f"plan_{secrets.token_hex(4)}",
            "status": "pending",
            "created_at": created.isoformat(timespec="seconds"),
            "expires_at": (created + self.ttl).isoformat(timespec="seconds"),
            **fields,
        }
        self.save(plan)
        return plan

    def save(self, plan: dict[str, Any]) -> None:
        path = self.plans_dir / f"{plan['plan_id']}.json"
        path.write_text(json.dumps(plan, indent=2))

    def load(self, plan_id: str) -> dict[str, Any]:
        if not _PLAN_ID.match(plan_id):
            raise PlanError(f"{plan_id!r} is not a plan id (expected 'plan_' + 8 hex chars).")
        path = self.plans_dir / f"{plan_id}.json"
        if not path.exists():
            raise PlanError(f"No plan {plan_id}. Build a new one with a gads_plan_* tool.")
        return json.loads(path.read_text())

    def load_pending(self, plan_id: str) -> dict[str, Any]:
        plan = self.load(plan_id)
        if plan["status"] != "pending":
            raise PlanError(f"Plan {plan_id} is already {plan['status']}. Build a new plan.")
        if now() > datetime.fromisoformat(plan["expires_at"]):
            plan["status"] = "expired"
            self.save(plan)
            raise PlanError(
                f"Plan {plan_id} expired at {plan['expires_at']}; the account may have "
                "changed since. Build a new plan."
            )
        return plan

    def recent(self, limit: int) -> list[dict[str, Any]]:
        files = sorted(self.plans_dir.glob("plan_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [json.loads(path.read_text()) for path in files[:limit]]

    def audit(self, event: str, **fields: Any) -> None:
        record = {"ts": now().isoformat(timespec="seconds"), "event": event, **fields}
        with self.audit_path.open("a") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
