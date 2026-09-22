import uuid
from datetime import date as _Date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContributionLinks(BaseModel):
    """The two real bank transactions a contribution is made of, as read.

    Almost always the two link columns as stored. The exception is
    history: a settlement recorded before the receiver side existed put
    the receiver's credit in the payer-side column, and is read here as
    the receiver side with no payer side. Nothing is rewritten for it, so
    `transaction_id` still shows what the column holds.
    """

    payer_transaction_id: Optional[uuid.UUID] = None
    receiver_transaction_id: Optional[uuid.UUID] = None


class GroupSettlementBase(BaseModel):
    from_member_id: uuid.UUID
    to_member_id: uuid.UUID
    amount: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    date: _Date
    # The payer side: the transaction on the account the money left.
    transaction_id: Optional[uuid.UUID] = None
    # The receiver side: the transaction on the account it landed on.
    receiver_transaction_id: Optional[uuid.UUID] = None
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _distinct_members(self):
        if self.from_member_id == self.to_member_id:
            raise ValueError("from_member_id and to_member_id must differ")
        return self


class GroupSettlementCreate(GroupSettlementBase):
    # When provided, also creates a debit transaction on this account
    # for the settlement amount and links it via `transaction_id`. The
    # account must belong to the requesting user, and the user must be
    # the `from_member` (the payer). Mutually exclusive with passing
    # `transaction_id` directly.
    account_id: Optional[uuid.UUID] = None
    description: Optional[str] = None
    # Ask for a synthetic credit on the receiver's first checking or
    # savings account. Off by default: a contribution is the real bank
    # transaction, and a second row for money an imported account already
    # shows would count it twice. Mutually exclusive with passing
    # `receiver_transaction_id`.
    create_receiver_transaction: bool = False


class GroupSettlementUpdate(BaseModel):
    from_member_id: Optional[uuid.UUID] = None
    to_member_id: Optional[uuid.UUID] = None
    amount: Optional[Decimal] = Field(default=None, gt=0)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)
    date: Optional[_Date] = None
    transaction_id: Optional[uuid.UUID] = None
    receiver_transaction_id: Optional[uuid.UUID] = None
    notes: Optional[str] = None


class MarkContributionFromTransaction(BaseModel):
    """Mark an existing transaction as a contribution.

    Only the other member is named: the amount, the currency, the date
    and which side of the contribution the transaction is are all read
    off the transaction and its account's owner.
    """

    transaction_id: uuid.UUID
    member_id: uuid.UUID
    notes: Optional[str] = None


class GroupSettlementRead(GroupSettlementBase):
    id: uuid.UUID
    group_id: uuid.UUID
    created_at: datetime
    links: ContributionLinks = ContributionLinks()

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def _links_default_to_the_columns(self):
        """A row read without the service's resolution still reports its
        links, taken straight from the columns."""
        if (
            self.links.payer_transaction_id is None
            and self.links.receiver_transaction_id is None
            and (self.transaction_id or self.receiver_transaction_id)
        ):
            self.links = ContributionLinks(
                payer_transaction_id=self.transaction_id,
                receiver_transaction_id=self.receiver_transaction_id,
            )
        return self
