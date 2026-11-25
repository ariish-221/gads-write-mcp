"""Plain-dict operations and their translation into MutateOperation protos."""

from __future__ import annotations

import difflib
import re
from typing import Any, Literal

from google.protobuf import json_format
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_CAMEL = re.compile(r"(?<=[a-z0-9])([A-Z])")
RESOURCE_NAME = re.compile(r"^customers/(\d{10})/[A-Za-z]+/[\w~.\-]+$")


class OperationError(ValueError):
    """The operation is malformed. The message says how to fix it."""


def snake(key: str) -> str:
    return _CAMEL.sub(lambda m: "_" + m.group(1).lower(), key)


def normalize(value: Any) -> Any:
    """Recursively snake_case dict keys so masks and protos agree on names."""
    if isinstance(value, dict):
        return {snake(k): normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


class Operation(BaseModel):
    """One change to one Google Ads resource."""

    model_config = ConfigDict(extra="forbid")

    resource: str = Field(
        description="Resource type in snake_case, e.g. 'campaign', 'campaign_budget', "
        "'ad_group', 'ad_group_ad', 'ad_group_criterion', 'campaign_criterion'."
    )
    action: Literal["create", "update", "remove"]
    data: dict[str, Any] | None = Field(
        default=None,
        description="Resource fields in snake_case, as in the Google Ads API reference. "
        "Required for create and update. Updates must include 'resource_name'. "
        "Enums are strings ('PAUSED'); money is micros (1.50 -> 1500000).",
    )
    resource_name: str | None = Field(
        default=None,
        description="Required for remove, e.g. 'customers/1234567890/adGroupCriteria/111~222'.",
    )
    update_mask: list[str] | None = Field(
        default=None,
        description="Optional explicit field paths for update. Defaults to every field "
        "present in 'data'. Only needed to clear a field.",
    )

    @field_validator("update_mask", mode="before")
    @classmethod
    def _split_mask(cls, value: Any) -> Any:
        """Google's REST docs write masks as one comma-separated string; accept that too."""
        if isinstance(value, str):
            return [path.strip() for path in value.split(",") if path.strip()]
        return value

    @model_validator(mode="after")
    def _check_shape(self) -> "Operation":
        self.resource = snake(self.resource.strip())
        if self.action == "remove":
            if not self.resource_name:
                raise ValueError("remove needs 'resource_name'")
            if self.data:
                raise ValueError("remove takes 'resource_name' only, not 'data'")
            return self
        if not self.data:
            raise ValueError(f"{self.action} needs 'data'")
        self.data = normalize(self.data)
        if self.action == "update" and not self.data.get("resource_name"):
            raise ValueError("update needs data.resource_name")
        return self

    @property
    def target(self) -> str | None:
        """Resource name this operation acts on. None for creates."""
        if self.action == "remove":
            return self.resource_name
        if self.action == "update":
            return self.data["resource_name"]
        return None

    @property
    def mask(self) -> list[str]:
        if self.action != "update":
            return []
        paths = self.update_mask or [p for p in leaf_paths(self.data) if p != "resource_name"]
        return [_wire_path(p) for p in paths]


def leaf_paths(data: dict[str, Any], prefix: str = "") -> list[str]:
    """Field paths for every value the caller set, including falsy ones."""
    paths: list[str] = []
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            paths.extend(leaf_paths(value, f"{path}."))
        else:
            paths.append(path)
    return paths


def _wire_path(path: str) -> str:
    """The Python protos rename reserved words ('type' -> 'type_'); the API and GAQL do not."""
    return ".".join(segment.rstrip("_") for segment in path.split("."))


def dig(data: dict[str, Any] | None, path: str) -> Any:
    for key in path.split("."):
        if not isinstance(data, dict):
            return None
        if key not in data:
            key = f"{key}_"
            if key not in data:
                return None
        data = data[key]
    return data


def walk(value: Any, key: str = ""):
    """Yield (key, leaf_value) for every leaf in a nested dict/list."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from walk(v, k)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item, key)
    else:
        yield key, value


def build(client: Any, op: Operation) -> Any:
    """Operation -> MutateOperation protobuf message."""
    message = client.get_type("MutateOperation")
    fields = message.DESCRIPTOR.fields_by_name
    field = f"{op.resource}_operation"
    if field not in fields:
        known = [name.removesuffix("_operation") for name in fields]
        close = difflib.get_close_matches(op.resource, known, n=3)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise OperationError(f"Unknown resource '{op.resource}'.{hint}")

    inner = getattr(message, field)
    if op.action not in inner.DESCRIPTOR.fields_by_name:
        supported = [a for a in ("create", "update", "remove") if a in inner.DESCRIPTOR.fields_by_name]
        raise OperationError(
            f"'{op.resource}' does not support {op.action}. Supported: {', '.join(supported)}."
        )

    if op.action == "remove":
        inner.remove = op.resource_name
        return message

    try:
        json_format.ParseDict(op.data, getattr(inner, op.action))
    except json_format.ParseError as exc:
        raise OperationError(
            f"Bad field in {op.resource} {op.action}: {exc}. Use snake_case field names "
            "from the Google Ads API reference; enums as strings; money in micros."
        ) from exc
    if op.action == "update":
        if not op.mask:
            raise OperationError("update changes nothing: 'data' only has resource_name.")
        inner.update_mask.paths.extend(op.mask)
    return message
