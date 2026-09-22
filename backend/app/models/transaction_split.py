import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.group import GroupMember
    from app.models.transaction import Transaction


class TransactionSplit(Base):
    __tablename__ = "transaction_splits"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="CASCADE")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    # RESTRICT: removing a member with active splits requires reassigning
    # or deleting those splits first. Group-level CASCADE still works
    # when no splits exist.
    group_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("group_members.id", ondelete="RESTRICT")
    )
    # The explicit payer of the whole transaction, when the derived one
    # (the owner of the account the transaction sits on) is wrong: cash,
    # an account outside the app, or a member with no Securo user. Null
    # means "derive it", which is the default.
    #
    # It is a property of the transaction, not of one share, and is
    # written identically on every share row of a transaction by
    # `split_service.replace_splits`, the single writer. It lives here
    # rather than on `transactions` because a payer only means something
    # relative to a group, and the group is only known through the
    # shares: a member on this side is guaranteed to be a member of the
    # group the shares belong to, and the override cannot outlive them.
    payer_group_member_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("group_members.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    # Always materialized in the parent transaction's currency. The
    # service layer assigns the rounding residual to the last share so
    # the sum is exact.
    share_amount: Mapped[Decimal] = mapped_column(Numeric(precision=15, scale=2))
    share_type: Mapped[str] = mapped_column(String(10), default="exact", server_default="exact")
    # Preserved only for share_type='percent' so the value round-trips
    # through edit. Otherwise null.
    share_pct: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(precision=5, scale=2), nullable=True
    )
    notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    transaction: Mapped["Transaction"] = relationship(back_populates="splits")
    # Two foreign keys point at group_members now, so both sides of the
    # share relationship have to say which one they mean.
    member: Mapped["GroupMember"] = relationship(
        back_populates="splits", foreign_keys=[group_member_id]
    )
