"""A group's common pot: positions per member, per currency, per period.

One calculation for every group. For a member and a period:

    position = shares - paid - contributions made + contributions received

A debit counts positive and a credit (shared income) negative, applied to
both the shares and what was paid. The payer of a transaction is credited
with the sum of its shares, not its amount, so positions sum to zero per
currency by construction, also when an amount was edited after sharing.

Identity is read from persisted columns only. The group service rewrites
`GroupMember.is_self` on loaded members per request for the "(you)" tag,
so this module never reads members as ORM entities: every viewer gets the
same positions, and the logged-in user is used for access control alone.

Periods are half-open, [start, end), over the reporting date the rest of
the app buckets by. The running position is everything up to `end`; the
backlog is the running position just before `start`.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.category import Category
from app.models.group import Group, GroupMember
from app.models.group_settlement import GroupSettlement
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.schemas.group_position import (
    CategoryLine,
    CategoryMemberShare,
    GroupPeriod,
    GroupPositions,
    MemberPosition,
    PeriodContribution,
    PeriodTransaction,
    PeriodTransactionPage,
    PositionMember,
    SuggestedTransfer,
)

ZERO = Decimal("0")

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


@dataclass(frozen=True)
class _Member:
    id: uuid.UUID
    name: str
    linked_user_id: Optional[uuid.UUID]


@dataclass(frozen=True)
class _GroupIdentity:
    group_id: uuid.UUID
    owner_user_id: uuid.UUID
    kind: str
    default_currency: str
    members: list[_Member]
    owner_member_id: Optional[uuid.UUID]


@dataclass
class _SharedTransaction:
    id: uuid.UUID
    report_date: date
    booked_date: date
    created_at: object
    description: str
    type: str
    amount: Decimal
    currency: str
    category_id: Optional[uuid.UUID]
    category_name: Optional[str]
    account_id: uuid.UUID
    account_name: Optional[str]
    payer_member_id: Optional[uuid.UUID]
    payer_assumed: bool
    # member id -> signed share
    shares: dict[uuid.UUID, Decimal] = field(default_factory=dict)

    @property
    def shared_total(self) -> Decimal:
        return sum(self.shares.values(), ZERO)


@dataclass
class _Tally:
    paid: Decimal = ZERO
    share: Decimal = ZERO
    made: Decimal = ZERO
    received: Decimal = ZERO

    @property
    def position(self) -> Decimal:
        return self.share - self.paid - self.made + self.received


async def _reporting_date_col(session: AsyncSession):
    """The one place period membership is decided for shared
    transactions: the same reporting date budgets and reports use."""
    from app.services._query_filters import reporting_date_col
    from app.services.admin_service import get_credit_card_accounting_mode

    return reporting_date_col(await get_credit_card_accounting_mode(session))


async def _load_identity(
    session: AsyncSession,
    group_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Optional[_GroupIdentity]:
    """Access control plus identity, from columns only.

    Selecting columns instead of entities keeps the identity map, and the
    per-request `is_self` the group service may have written into it, out
    of the calculation.
    """
    from app.services.group_service import _visible_predicate

    group_row = (
        await session.execute(
            select(Group.id, Group.user_id, Group.kind, Group.default_currency).where(
                Group.id == group_id, _visible_predicate(workspace_id, user_id)
            )
        )
    ).first()
    if group_row is None:
        return None

    member_rows = (
        await session.execute(
            select(
                GroupMember.id,
                GroupMember.name,
                GroupMember.linked_user_id,
                GroupMember.is_self,
            )
            .where(GroupMember.group_id == group_id)
            .order_by(GroupMember.created_at, GroupMember.id)
        )
    ).all()

    # The owner's member: the one linked to the group's owner; failing
    # that, the one carrying the stored owner flag.
    owner_member_id: Optional[uuid.UUID] = None
    linked_to_owner = [r for r in member_rows if r.linked_user_id == group_row.user_id]
    if linked_to_owner:
        flagged = [r for r in linked_to_owner if r.is_self]
        owner_member_id = (flagged or linked_to_owner)[0].id
    else:
        flagged = [r for r in member_rows if r.is_self]
        if flagged:
            owner_member_id = flagged[0].id

    return _GroupIdentity(
        group_id=group_row.id,
        owner_user_id=group_row.user_id,
        kind=group_row.kind,
        default_currency=group_row.default_currency,
        members=[_Member(r.id, r.name, r.linked_user_id) for r in member_rows],
        owner_member_id=owner_member_id,
    )


def _resolve_payer(
    identity: _GroupIdentity, account_owner_id: Optional[uuid.UUID]
) -> tuple[Optional[uuid.UUID], bool]:
    """Return (payer member, payer assumed) for a transaction on an
    account owned by `account_owner_id`.

    The payer is the member linked to the account's owner, or the owner's
    member when that user owns the group. When the account's owner is no
    member, or several members link that user, the payer falls back to
    the owner's member and is flagged as assumed. In a group with no
    owner's member the fallback is nobody: shares count and the owner's
    side stays implicit.
    """
    linked = [m for m in identity.members if m.linked_user_id == account_owner_id]
    if account_owner_id is not None and len(linked) > 1:
        return identity.owner_member_id, True
    if account_owner_id == identity.owner_user_id:
        return identity.owner_member_id, False
    if len(linked) == 1:
        return linked[0].id, False
    return identity.owner_member_id, True


async def _load_shared_transactions(
    session: AsyncSession, identity: _GroupIdentity, end: Optional[date]
) -> list[_SharedTransaction]:
    """Every transaction with shares in the group, whatever its status or
    P/L flags, up to `end`. Newest first."""
    report_date = await _reporting_date_col(session)
    query = (
        select(
            TransactionSplit.group_member_id,
            TransactionSplit.share_amount,
            Transaction.id,
            Transaction.date,
            report_date.label("report_date"),
            Transaction.created_at,
            Transaction.description,
            Transaction.type,
            Transaction.amount,
            Transaction.currency,
            Transaction.category_id,
            Category.name.label("category_name"),
            Transaction.account_id,
            Account.name.label("account_name"),
            Account.user_id.label("account_owner_id"),
        )
        .join(Transaction, TransactionSplit.transaction_id == Transaction.id)
        .join(GroupMember, TransactionSplit.group_member_id == GroupMember.id)
        .outerjoin(Account, Transaction.account_id == Account.id)
        .outerjoin(Category, Transaction.category_id == Category.id)
        .where(GroupMember.group_id == identity.group_id)
    )
    if end is not None:
        query = query.where(report_date < end)

    by_id: dict[uuid.UUID, _SharedTransaction] = {}
    for row in (await session.execute(query)).all():
        tx = by_id.get(row.id)
        if tx is None:
            payer_member_id, payer_assumed = _resolve_payer(identity, row.account_owner_id)
            tx = _SharedTransaction(
                id=row.id,
                report_date=row.report_date,
                booked_date=row.date,
                created_at=row.created_at,
                description=row.description,
                type=row.type,
                amount=Decimal(str(row.amount)),
                currency=row.currency,
                category_id=row.category_id,
                category_name=row.category_name,
                account_id=row.account_id,
                account_name=row.account_name,
                payer_member_id=payer_member_id,
                payer_assumed=payer_assumed,
            )
            by_id[row.id] = tx
        sign = Decimal("-1") if row.type == "credit" else Decimal("1")
        tx.shares[row.group_member_id] = (
            tx.shares.get(row.group_member_id, ZERO) + sign * Decimal(str(row.share_amount))
        )

    return sorted(
        by_id.values(),
        key=lambda t: (t.report_date, str(t.created_at), str(t.id)),
        reverse=True,
    )


async def _load_contributions(
    session: AsyncSession, identity: _GroupIdentity, end: Optional[date]
) -> list[PeriodContribution]:
    """Contributions live in the settlement storage and count in the
    period they are dated in. Newest first."""
    query = select(
        GroupSettlement.id,
        GroupSettlement.from_member_id,
        GroupSettlement.to_member_id,
        GroupSettlement.amount,
        GroupSettlement.currency,
        GroupSettlement.date,
        GroupSettlement.transaction_id,
        GroupSettlement.receiver_transaction_id,
        GroupSettlement.notes,
        GroupSettlement.created_at,
    ).where(GroupSettlement.group_id == identity.group_id)
    if end is not None:
        query = query.where(GroupSettlement.date < end)
    rows = sorted(
        (await session.execute(query)).all(),
        key=lambda r: (r.date, str(r.created_at), str(r.id)),
        reverse=True,
    )
    return [
        PeriodContribution(
            id=r.id,
            from_member_id=r.from_member_id,
            to_member_id=r.to_member_id,
            amount=Decimal(str(r.amount)),
            currency=r.currency,
            date=r.date,
            transaction_id=r.transaction_id,
            receiver_transaction_id=r.receiver_transaction_id,
            notes=r.notes,
        )
        for r in rows
    ]


def suggest_transfers(
    positions: dict[uuid.UUID, Decimal], member_order: list[uuid.UUID], currency: str
) -> list[SuggestedTransfer]:
    """A small deterministic set for one currency: the largest debtor
    pays the largest creditor until one side runs out. Ties break on the
    members' stable order (creation time)."""
    rank = {member_id: i for i, member_id in enumerate(member_order)}
    debtors = {m: a for m, a in positions.items() if a > 0}
    creditors = {m: -a for m, a in positions.items() if a < 0}
    transfers: list[SuggestedTransfer] = []
    while debtors and creditors:
        debtor = min(debtors, key=lambda m: (-debtors[m], rank.get(m, len(rank))))
        creditor = min(creditors, key=lambda m: (-creditors[m], rank.get(m, len(rank))))
        amount = min(debtors[debtor], creditors[creditor])
        transfers.append(
            SuggestedTransfer(
                from_member_id=debtor, to_member_id=creditor, currency=currency, amount=amount
            )
        )
        for pot, member_id in ((debtors, debtor), (creditors, creditor)):
            pot[member_id] -= amount
            if pot[member_id] == 0:
                del pot[member_id]
    return transfers


def _in_period(d: date, start: Optional[date]) -> bool:
    # The upper bound is applied when loading.
    return start is None or d >= start


async def _build_positions(
    session: AsyncSession,
    identity: _GroupIdentity,
    transactions: list[_SharedTransaction],
    contributions: list[PeriodContribution],
    start: Optional[date],
    end: Optional[date],
) -> GroupPositions:
    from app.services.fx_rate_service import convert

    # (member, currency) -> tallies, before the period and within it.
    before: dict[tuple[uuid.UUID, str], _Tally] = defaultdict(_Tally)
    period: dict[tuple[uuid.UUID, str], _Tally] = defaultdict(_Tally)
    currencies: set[str] = set()

    for tx in transactions:
        bucket = period if _in_period(tx.report_date, start) else before
        currencies.add(tx.currency)
        for member_id, amount in tx.shares.items():
            bucket[(member_id, tx.currency)].share += amount
        if tx.payer_member_id is not None:
            bucket[(tx.payer_member_id, tx.currency)].paid += tx.shared_total

    for c in contributions:
        bucket = period if _in_period(c.date, start) else before
        currencies.add(c.currency)
        bucket[(c.from_member_id, c.currency)].made += c.amount
        bucket[(c.to_member_id, c.currency)].received += c.amount

    if not currencies:
        currencies.add(identity.default_currency)

    member_order = [m.id for m in identity.members]
    positions: list[MemberPosition] = []
    transfers_period: list[SuggestedTransfer] = []
    transfers_running: list[SuggestedTransfer] = []

    for currency in sorted(currencies):
        period_by_member: dict[uuid.UUID, Decimal] = {}
        running_by_member: dict[uuid.UUID, Decimal] = {}
        for member_id in member_order:
            tally = period[(member_id, currency)]
            backlog = before[(member_id, currency)].position
            running = backlog + tally.position
            period_by_member[member_id] = tally.position
            running_by_member[member_id] = running

            converted = []
            for amount in (tally.position, running):
                if currency == identity.default_currency or amount == 0:
                    converted.append(amount)
                else:
                    value, _ = await convert(
                        session, amount, currency, identity.default_currency
                    )
                    converted.append(value)

            positions.append(
                MemberPosition(
                    member_id=member_id,
                    currency=currency,
                    paid=tally.paid,
                    share=tally.share,
                    contributions_made=tally.made,
                    contributions_received=tally.received,
                    period_position=tally.position,
                    backlog=backlog,
                    running_position=running,
                    period_position_in_default_currency=converted[0],
                    running_position_in_default_currency=converted[1],
                )
            )
        transfers_period += suggest_transfers(period_by_member, member_order, currency)
        transfers_running += suggest_transfers(running_by_member, member_order, currency)

    return GroupPositions(
        group_id=identity.group_id,
        kind=identity.kind,
        default_currency=identity.default_currency,
        start=start,
        end=end,
        owner_member_id=identity.owner_member_id,
        members=[
            PositionMember(
                id=m.id, name=m.name, is_owner_member=m.id == identity.owner_member_id
            )
            for m in identity.members
        ],
        positions=positions,
        transfers_period=transfers_period,
        transfers_running=transfers_running,
    )


def _category_lines(
    transactions: list[_SharedTransaction], member_order: list[uuid.UUID], income: bool
) -> list[CategoryLine]:
    """Costs (debits) or shared income (credits) by category and
    currency, as magnitudes, with each member's share."""
    totals: dict[tuple[Optional[uuid.UUID], str], dict[uuid.UUID, Decimal]] = {}
    names: dict[Optional[uuid.UUID], Optional[str]] = {}
    for tx in transactions:
        if (tx.type == "credit") != income:
            continue
        names[tx.category_id] = tx.category_name
        by_member = totals.setdefault((tx.category_id, tx.currency), defaultdict(lambda: ZERO))
        for member_id, amount in tx.shares.items():
            by_member[member_id] += abs(amount)

    lines = [
        CategoryLine(
            category_id=category_id,
            category_name=names[category_id],
            currency=currency,
            total=sum(by_member.values(), ZERO),
            shares=[
                CategoryMemberShare(member_id=m, amount=by_member[m])
                for m in member_order
                if m in by_member
            ],
        )
        for (category_id, currency), by_member in totals.items()
    ]
    return sorted(
        lines, key=lambda ln: (ln.currency, -ln.total, ln.category_name or "", str(ln.category_id))
    )


def _to_period_transaction(
    tx: _SharedTransaction, member_order: list[uuid.UUID]
) -> PeriodTransaction:
    return PeriodTransaction(
        id=tx.id,
        date=tx.report_date,
        booked_date=tx.booked_date,
        description=tx.description,
        type=tx.type,
        amount=tx.amount,
        currency=tx.currency,
        category_id=tx.category_id,
        category_name=tx.category_name,
        account_id=tx.account_id,
        account_name=tx.account_name,
        payer_member_id=tx.payer_member_id,
        payer_assumed=tx.payer_assumed,
        shared_total=tx.shared_total,
        shares=[
            CategoryMemberShare(member_id=m, amount=tx.shares[m])
            for m in member_order
            if m in tx.shares
        ],
    )


async def compute_positions(
    session: AsyncSession,
    group_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> Optional[GroupPositions]:
    """Positions for every member of a group over [start, end).

    `workspace_id` and `user_id` decide only whether the group is visible
    (None when it is not); the figures are the same for every viewer.
    """
    identity = await _load_identity(session, group_id, workspace_id, user_id)
    if identity is None:
        return None
    transactions = await _load_shared_transactions(session, identity, end)
    contributions = await _load_contributions(session, identity, end)
    return await _build_positions(session, identity, transactions, contributions, start, end)


async def compute_period(
    session: AsyncSession,
    group_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    start: Optional[date] = None,
    end: Optional[date] = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Optional[GroupPeriod]:
    """Everything the group page shows for [start, end), from one pass
    over the same rows the positions come from."""
    identity = await _load_identity(session, group_id, workspace_id, user_id)
    if identity is None:
        return None
    transactions = await _load_shared_transactions(session, identity, end)
    contributions = await _load_contributions(session, identity, end)
    positions = await _build_positions(
        session, identity, transactions, contributions, start, end
    )

    member_order = [m.id for m in identity.members]
    in_period = [tx for tx in transactions if _in_period(tx.report_date, start)]
    page = max(page, 1)
    page_size = min(max(page_size, 1), MAX_PAGE_SIZE)
    offset = (page - 1) * page_size

    return GroupPeriod(
        **positions.model_dump(),
        costs=_category_lines(in_period, member_order, income=False),
        shared_income=_category_lines(in_period, member_order, income=True),
        contributions=[c for c in contributions if _in_period(c.date, start)],
        payer_assumed_transactions=[
            _to_period_transaction(tx, member_order) for tx in in_period if tx.payer_assumed
        ],
        transactions=PeriodTransactionPage(
            items=[
                _to_period_transaction(tx, member_order)
                for tx in in_period[offset : offset + page_size]
            ],
            total=len(in_period),
            page=page,
            page_size=page_size,
        ),
    )
