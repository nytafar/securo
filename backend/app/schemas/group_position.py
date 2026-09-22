"""Response shapes for a group's common pot over a period.

Sign convention, used everywhere below: a debit counts positive and a
credit (shared income) negative. A positive position means the member
should still move money into the pot; a negative one means the pot owes
them. Positions sum to zero per currency whenever the group has an
owner's member.
"""

import uuid
from datetime import date as _Date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class PositionMember(BaseModel):
    id: uuid.UUID
    name: str
    # True for the member that stands for the group's owner, resolved
    # from persisted columns and therefore the same for every viewer.
    is_owner_member: bool


class MemberPosition(BaseModel):
    member_id: uuid.UUID
    currency: str
    # Signed sum of the shares of the period's transactions this member paid.
    paid: Decimal
    # Signed sum of this member's own shares in the period.
    share: Decimal
    contributions_made: Decimal
    contributions_received: Decimal
    # share - paid - contributions_made + contributions_received
    period_position: Decimal
    # The running position just before the period starts.
    backlog: Decimal
    # backlog + period_position: everything up to the period's end.
    running_position: Decimal
    # Display only; never used to suggest transfers.
    period_position_in_default_currency: Decimal
    running_position_in_default_currency: Decimal


class SuggestedTransfer(BaseModel):
    from_member_id: uuid.UUID
    to_member_id: uuid.UUID
    currency: str
    amount: Decimal


class CategoryMemberShare(BaseModel):
    member_id: uuid.UUID
    amount: Decimal


class CategoryLine(BaseModel):
    category_id: Optional[uuid.UUID] = None
    category_name: Optional[str] = None
    currency: str
    # Magnitude, always positive: in `costs` it is what the category
    # cost, in `shared_income` what came in.
    total: Decimal
    shares: list[CategoryMemberShare] = []


class PeriodTotal(BaseModel):
    """The period's totals for one currency, netted here so no reader has
    to subtract shared income from costs itself."""

    currency: str
    # Magnitudes: what the period's debits came to, and its credits.
    costs: Decimal
    shared_income: Decimal
    # costs - shared_income: what the pot carried, and what the members'
    # shares add up to.
    net: Decimal
    # One entry per member, in member order, signed like `net`.
    shares: list[CategoryMemberShare] = []


class PeriodContribution(BaseModel):
    id: uuid.UUID
    from_member_id: uuid.UUID
    to_member_id: uuid.UUID
    amount: Decimal
    currency: str
    date: _Date
    transaction_id: Optional[uuid.UUID] = None
    receiver_transaction_id: Optional[uuid.UUID] = None
    notes: Optional[str] = None


class PeriodTransaction(BaseModel):
    id: uuid.UUID
    # The reporting date that decides the period; `booked_date` is the
    # transaction's own date.
    date: _Date
    booked_date: _Date
    description: str
    type: str
    amount: Decimal
    currency: str
    category_id: Optional[uuid.UUID] = None
    category_name: Optional[str] = None
    account_id: uuid.UUID
    account_name: Optional[str] = None
    # None only in a group with no owner's member to fall back on.
    payer_member_id: Optional[uuid.UUID] = None
    payer_assumed: bool = False
    # Signed sum of the shares: what the payer is credited with.
    shared_total: Decimal
    shares: list[CategoryMemberShare] = []


class PeriodTransactionPage(BaseModel):
    items: list[PeriodTransaction] = []
    total: int
    page: int
    page_size: int


class GroupPositions(BaseModel):
    group_id: uuid.UUID
    kind: str
    default_currency: str
    # Half-open range [start, end). None means unbounded on that side.
    start: Optional[_Date] = None
    end: Optional[_Date] = None
    owner_member_id: Optional[uuid.UUID] = None
    members: list[PositionMember] = []
    positions: list[MemberPosition] = []
    # Settles the period alone.
    transfers_period: list[SuggestedTransfer] = []
    # Settles the period and the backlog.
    transfers_running: list[SuggestedTransfer] = []


class GroupPeriod(GroupPositions):
    costs: list[CategoryLine] = []
    shared_income: list[CategoryLine] = []
    totals: list[PeriodTotal] = []
    contributions: list[PeriodContribution] = []
    payer_assumed_transactions: list[PeriodTransaction] = []
    transactions: PeriodTransactionPage
