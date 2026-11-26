from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.enums import TargetKind
from app.models._constants import check_in
from app.models._mixins import CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin


class Target(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """Something sharpwire plans for, synced from the intent on every reconcile. Never deleted: a device that leaves
    NetBox stays as `present = false`, so its plans and alerts keep their target."""

    __tablename__ = "targets"
    __table_args__ = (
        UniqueConstraint("name", name="uq_targets_name"),
        check_in("kind", TargetKind, "ck_target_kind"),
    )

    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    # Where the driver reaches it: the mgmt address from NetBox.
    address: Mapped[str] = mapped_column(String, nullable=False)
    # NetBox's sharpwire_managed. Only managed targets are planned.
    managed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Whether it's in the intent the last reconcile built.
    present: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # When it last became planned (present and managed): "overdue" counts from here for a target that came back.
    planned_since: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # The last reconcile that read the target and planned it, and why the latest one couldn't, if it couldn't.
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String)
