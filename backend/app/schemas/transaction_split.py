import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

ShareType = Literal["equal", "exact", "percent"]


class TransactionSplitInput(BaseModel):
    """One row in a splits payload. Required fields depend on share_type:
    equal -> only group_member_id; exact -> + share_amount; percent ->
    + share_pct."""

    group_member_id: uuid.UUID
    share_amount: Optional[Decimal] = None
    share_pct: Optional[Decimal] = None
    notes: Optional[str] = None


class TransactionSplitsInput(BaseModel):
    """Whole splits payload attached to a transaction."""

    share_type: ShareType
    splits: list[TransactionSplitInput]
    # The member who actually paid, when the owner of the account the
    # transaction sits on is not it: cash, an account outside the app, a
    # member with no Securo user. Omit it (or send null) to derive the
    # payer as before — the payload replaces the sharing wholesale, so
    # leaving it out is how an override is cleared. The member must
    # belong to the same group as the shares.
    payer_group_member_id: Optional[uuid.UUID] = None


class TransactionSplitRead(BaseModel):
    id: uuid.UUID
    transaction_id: uuid.UUID
    group_member_id: uuid.UUID
    # The explicit payer of the transaction, or null when it is derived
    # from the account's owner. The same on every share row.
    payer_group_member_id: Optional[uuid.UUID] = None
    share_amount: Decimal
    share_type: str
    share_pct: Optional[Decimal] = None
    notes: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
