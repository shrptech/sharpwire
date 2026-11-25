"""The plan a driver produces for one target: what differs from the intent, as ordered ops.

Every driver returns a PlanDraft. An empty one means the target matches NetBox; a non-empty one is drift
(docs/06-drift-and-approvals.md). The controller stores a draft as an immutable, hashed plan (step 3c), and
applies it once a person approves it (step 5).
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.enums import OpAction, PlanStatus


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Op(_Frozen):
    """One change to one object, which is named by its menu or kind (`path`) and its natural key.

    - `add`: `fields` holds every field to set; `place_before` positions it in an ordered menu.
    - `set`: `fields` holds the new values and `before` the old ones. None means the field goes back to its
      default (RouterOS leaves a field at its default out of a GET).
    - `remove`: `before` holds the managed fields as they were.
    - `move`: puts the object before `place_before`, or at the end when that's None.
    """

    action: OpAction
    path: str
    key: dict[str, str]
    fields: dict[str, str | None] = {}
    before: dict[str, str | None] = {}
    place_before: dict[str, str] | None = None

    def describe(self) -> str:
        """One readable line for the plan's diff."""
        where = f"/{self.path}" + "".join(f" {k}={_quote(v)}" for k, v in self.key.items())
        match self.action:
            case "add":
                extra = " ".join(f"{k}={_quote(v)}" for k, v in self.fields.items() if k not in self.key)
                return f"+ {where} {extra}".rstrip() + _position(self.place_before)
            case "set":
                changes = ", ".join(f"{k}: {_show(self.before.get(k))} -> {_show(v)}" for k, v in self.fields.items())
                return f"~ {where}: {changes}"
            case "remove":
                return f"- {where}"
            case "move":
                return f"^ {where}" + (_position(self.place_before) or " (to the end)")


def _quote(value: str | None) -> str:
    if value is None:
        return "(default)"
    return f'"{value}"' if not value or any(c.isspace() for c in value) else value


def _show(value: str | None) -> str:
    return _quote(value)


def _position(before: dict[str, str] | None) -> str:
    if before is None:
        return ""
    return " (before " + " ".join(f"{k}={_quote(v)}" for k, v in before.items()) + ")"


class PlanWarning(_Frozen):
    """A legal but risky change the approver must read (docs/02-intent-model.md#plan-warnings)."""

    code: str
    message: str


class Unmanaged(_Frozen):
    """A row in a menu the driver renders into that sharpwire doesn't own. Reported, never changed."""

    path: str
    key: dict[str, str]


class PlanDraft(_Frozen):
    target: str
    ops: tuple[Op, ...]
    # Applied in order, they undo `ops` on the live state the plan was built from. The armed rollback is built
    # from them (docs/05-apply-and-rollout.md).
    inverse_ops: tuple[Op, ...]
    # A hash of the live state the plan was built from, for the stale check.
    fingerprint: str
    warnings: tuple[PlanWarning, ...] = ()
    # Hard guards (docs/03-pipeline-and-validation.md#hard-guards). A plan with any can never be approved.
    guard_violations: tuple[str, ...] = ()
    unmanaged: tuple[Unmanaged, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.ops

    @property
    def diff(self) -> str:
        return "".join(f"{op.describe()}\n" for op in self.ops)


# --- Wire shapes for stored plans ------------------------------------------------------------------------------


class PlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    target_id: uuid.UUID
    status: PlanStatus
    hash: str
    fingerprint: str
    netbox_change_id: int | None
    diff: str
    ops: list[Op]
    inverse_ops: list[Op]
    warnings: list[PlanWarning]
    guard_violations: list[str]
    unmanaged: list[Unmanaged]
    created_at: datetime
    last_seen_at: datetime
    superseded_at: datetime | None


class PlanPage(BaseModel):
    items: list[PlanResponse]
    total: int
