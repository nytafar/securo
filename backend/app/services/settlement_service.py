"""Contributions — money a member moves to carry their part of the pot.

They live in the settlement storage, and the two link columns mean payer
side (`transaction_id`) and receiver side (`receiver_transaction_id`). A
contribution is never a copy of a transaction: the real bank row is
linked, and a synthetic one is written only when the caller asks for it.

One transaction belongs to at most one contribution, across both columns,
and a transaction that carries group shares cannot also be one — a share
and a contribution are two different things the same money would
otherwise be counted as.

History needs no migration: a settlement recorded before the receiver
side existed links the receiver's credit in the payer-side column, and is
*read* as a receiver-side link. Nothing is rewritten for it.
"""

import uuid
from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.group import Group, GroupMember
from app.models.group_settlement import GroupSettlement
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.schemas.group_settlement import (
    ContributionLinks,
    GroupSettlementCreate,
    GroupSettlementUpdate,
    MarkContributionFromTransaction,
)

# A transaction this service wrote itself, rather than one the bank sent.
# Deleting a contribution may remove its own synthetic rows and nothing
# else: a bank row is the user's record of money that really moved.
SYNTHETIC_SOURCE = "settlement"


async def _ensure_group_visible(
    session: AsyncSession,
    group_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Optional[Group]:
    """Visible when the group lives in the current workspace OR the
    caller is linked as a cross-workspace member — for read endpoints."""
    from app.services.group_service import get_group_visible

    return await get_group_visible(session, group_id, workspace_id, user_id)


async def _user_member_id(
    session: AsyncSession, group_id: uuid.UUID, user_id: uuid.UUID
) -> Optional[uuid.UUID]:
    """If the user is a linked member of this group, return that member
    id. Owners may not have a linked member (they can still act via
    the owner check), so this can return None for them."""
    result = await session.execute(
        select(GroupMember.id).where(
            GroupMember.group_id == group_id,
            GroupMember.linked_user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def _can_settle_from(
    session: AsyncSession,
    group: Group,
    user_id: uuid.UUID,
    from_member_id: uuid.UUID,
) -> bool:
    """Permission check for creating/editing a settlement:
    - Group owner can do anything.
    - Linked member can only act when they are the `from_member`
      (i.e., they're recording a payment they themselves made)."""
    if group.user_id == user_id:
        return True
    linked = await _user_member_id(session, group.id, user_id)
    return linked is not None and linked == from_member_id


async def _create_payment_transaction(
    session: AsyncSession,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    account_id: uuid.UUID,
    amount,
    currency: str,
    when,
    description: str,
) -> Transaction:
    """Create a debit transaction on the user's account representing a
    settlement payment. Validates that the account belongs to the
    current workspace."""
    account_result = await session.execute(
        select(Account).where(
            Account.id == account_id,
            Account.workspace_id == workspace_id,
        )
    )
    account = account_result.scalar_one_or_none()
    if account is None:
        raise ValueError("Account not found")

    tx = Transaction(
        id=uuid.uuid4(),
        user_id=user_id,
        workspace_id=workspace_id,
        account_id=account.id,
        description=description,
        amount=amount,
        currency=currency,
        date=when,
        type="debit",
        # `settlement` is a special source that excludes the row from
        # spending reports — the underlying expense is already counted
        # via the share that produced the debt; this is the payback,
        # not a new expense.
        source="settlement",
        created_at=datetime.now(timezone.utc),
    )
    session.add(tx)
    await session.flush()
    # Stamp primary-currency amount so dashboard / report aggregations
    # that prefer amount_primary include this row.
    from app.services.fx_rate_service import stamp_primary_amount

    await stamp_primary_amount(session, user_id, tx)
    return tx


async def _pick_default_account_for_user(
    session: AsyncSession, user_id: uuid.UUID
) -> Optional[Account]:
    """Return the user's first non-archived checking/savings account
    across any workspace they belong to. Used as the auto-target for
    receiver-side settlement credits — the receiver may sit in a
    different workspace than where the settlement was recorded."""
    from app.models.workspace import WorkspaceMember

    # Resolve the workspaces the user can write to. Pick the first
    # account that lives in any of them; ties broken by name.
    user_workspaces_subq = select(WorkspaceMember.workspace_id).where(
        WorkspaceMember.user_id == user_id
    )
    result = await session.execute(
        select(Account)
        .where(
            Account.workspace_id.in_(user_workspaces_subq),
            Account.is_closed.is_(False),
            Account.type.in_(("checking", "savings")),
        )
        .order_by(Account.name)
    )
    return result.scalars().first()


async def _create_receiver_credit(
    session: AsyncSession,
    receiver_user_id: uuid.UUID,
    amount,
    currency: str,
    when,
    description: str,
) -> Optional[Transaction]:
    """Mirror a settlement credit on the receiver's side.

    Picks the receiver's first checking/savings account from any
    workspace they belong to and stamps the credit there. Returns None
    silently when the receiver has no suitable account — they can do
    it manually or the next time they reconcile from their bank sync.
    """
    account = await _pick_default_account_for_user(session, receiver_user_id)
    if account is None:
        return None
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=receiver_user_id,
        workspace_id=account.workspace_id,
        account_id=account.id,
        description=description,
        amount=amount,
        currency=currency,
        date=when,
        type="credit",
        # `settlement` marks the row as this service's own, so deleting
        # the contribution takes it with it. It is linked as the receiver
        # side, and a linked row is out of P/L for both members — see
        # `counts_as_pnl` — where an unlinked settlement credit still
        # counts as income for whoever received it.
        source="settlement",
        created_at=datetime.now(timezone.utc),
    )
    session.add(tx)
    await session.flush()
    from app.services.fx_rate_service import stamp_primary_amount

    await stamp_primary_amount(session, receiver_user_id, tx)
    return tx


async def _validate_members_in_group(
    session: AsyncSession, group_id: uuid.UUID, member_ids: list[uuid.UUID]
) -> None:
    result = await session.execute(
        select(GroupMember.id).where(
            GroupMember.group_id == group_id, GroupMember.id.in_(member_ids)
        )
    )
    found = {row[0] for row in result.all()}
    if found != set(member_ids):
        raise ValueError("Settlement members must belong to the group")


async def _validate_transaction(
    session: AsyncSession,
    transaction_id: Optional[uuid.UUID],
    workspace_id: uuid.UUID,
    settlement_id: Optional[uuid.UUID] = None,
) -> None:
    """The transaction exists here, is free, and carries no shares.

    `settlement_id` is the row being written, so re-saving a settlement
    with the link it already holds is not a clash with itself.
    """
    if transaction_id is None:
        return
    result = await session.execute(
        select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.workspace_id == workspace_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise ValueError("Linked transaction not found")
    await _assert_transaction_unlinked(session, transaction_id, settlement_id)
    await _assert_transaction_unshared(session, transaction_id)


async def _assert_transaction_unlinked(
    session: AsyncSession,
    transaction_id: uuid.UUID,
    settlement_id: Optional[uuid.UUID] = None,
) -> None:
    """One transaction, one contribution — across both link columns.

    Checked here, inside the caller's database transaction, because no
    single unique index can span two columns of the same table. The two
    unique indexes behind it (migration 091) catch the same transaction
    twice in one column, which is the race this check cannot see.
    """
    query = select(GroupSettlement.id).where(
        or_(
            GroupSettlement.transaction_id == transaction_id,
            GroupSettlement.receiver_transaction_id == transaction_id,
        )
    )
    if settlement_id is not None:
        query = query.where(GroupSettlement.id != settlement_id)
    if (await session.execute(query.limit(1))).first() is not None:
        raise ValueError("That transaction is already linked to a contribution")


async def _assert_transaction_unshared(
    session: AsyncSession, transaction_id: uuid.UUID
) -> None:
    """A transaction cannot both carry shares and be a contribution.

    Its shares say what the pot spent; a contribution says what a member
    put into the pot. The same money can only be one of the two, and
    letting a row be both would move every position twice.
    """
    shared = await session.execute(
        select(TransactionSplit.id)
        .where(TransactionSplit.transaction_id == transaction_id)
        .limit(1)
    )
    if shared.first() is not None:
        raise ValueError(
            "A transaction that is shared in a group cannot also be a contribution"
        )


async def is_contribution_link(
    session: AsyncSession, transaction_id: uuid.UUID
) -> bool:
    """True when this transaction is already a contribution's bank row.

    The other half of the guard, for the writers of shares.
    """
    result = await session.execute(
        select(GroupSettlement.id)
        .where(
            or_(
                GroupSettlement.transaction_id == transaction_id,
                GroupSettlement.receiver_transaction_id == transaction_id,
            )
        )
        .limit(1)
    )
    return result.first() is not None


async def linked_transaction_ids(
    session: AsyncSession, transaction_ids: Iterable[uuid.UUID]
) -> set[uuid.UUID]:
    """The subset of `transaction_ids` that a contribution already links,
    on either side. For callers that hold many rows at once."""
    ids = list(transaction_ids)
    if not ids:
        return set()
    result = await session.execute(
        select(GroupSettlement.transaction_id, GroupSettlement.receiver_transaction_id).where(
            or_(
                GroupSettlement.transaction_id.in_(ids),
                GroupSettlement.receiver_transaction_id.in_(ids),
            )
        )
    )
    wanted = set(ids)
    found: set[uuid.UUID] = set()
    for payer_id, receiver_id in result.all():
        for candidate in (payer_id, receiver_id):
            if candidate in wanted:
                found.add(candidate)
    return found


def _resolve_links(
    transaction_id: Optional[uuid.UUID],
    receiver_transaction_id: Optional[uuid.UUID],
    payer_side_type: Optional[str],
) -> ContributionLinks:
    """Which linked transaction is the payer's and which the receiver's.

    The columns say it, except for history: settlements recorded before
    the receiver side existed put the receiver's credit in the payer-side
    column. A credit sitting alone there is read as the receiver side, so
    the row counts once and on the side it really happened on. Read-time
    only — the stored row is never touched.
    """
    if (
        transaction_id is not None
        and receiver_transaction_id is None
        and payer_side_type == "credit"
    ):
        return ContributionLinks(
            payer_transaction_id=None, receiver_transaction_id=transaction_id
        )
    return ContributionLinks(
        payer_transaction_id=transaction_id,
        receiver_transaction_id=receiver_transaction_id,
    )


async def resolve_links(
    session: AsyncSession,
    rows: Sequence[tuple[Optional[uuid.UUID], Optional[uuid.UUID]]],
) -> list[ContributionLinks]:
    """`_resolve_links` for many (payer column, receiver column) pairs,
    with one query for the payer-side transactions' types."""
    payer_ids = [payer for payer, _ in rows if payer is not None]
    types: dict[uuid.UUID, str] = {}
    if payer_ids:
        result = await session.execute(
            select(Transaction.id, Transaction.type).where(Transaction.id.in_(payer_ids))
        )
        types = {row.id: row.type for row in result.all()}
    return [
        _resolve_links(payer, receiver, types.get(payer) if payer else None)
        for payer, receiver in rows
    ]


async def attach_links(
    session: AsyncSession, settlements: Sequence[GroupSettlement]
) -> None:
    """Stamp `links` on loaded settlements so the read schema carries the
    payer and receiver sides as resolved, next to the raw columns."""
    resolved = await resolve_links(
        session, [(s.transaction_id, s.receiver_transaction_id) for s in settlements]
    )
    for settlement, links in zip(settlements, resolved):
        # Not a column: the read schema picks it up from the instance,
        # the way the group service stamps `is_self` on loaded members.
        setattr(settlement, "links", links)


async def list_settlements(
    session: AsyncSession,
    group_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Optional[list[GroupSettlement]]:
    if not await _ensure_group_visible(session, group_id, workspace_id, user_id):
        return None
    result = await session.execute(
        select(GroupSettlement)
        .where(GroupSettlement.group_id == group_id)
        .order_by(GroupSettlement.date.desc(), GroupSettlement.created_at.desc())
    )
    settlements = list(result.scalars().all())
    await attach_links(session, settlements)
    return settlements


async def create_settlement(
    session: AsyncSession,
    group_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    data: GroupSettlementCreate,
) -> Optional[GroupSettlement]:
    group = await _ensure_group_visible(session, group_id, workspace_id, user_id)
    if not group:
        return None

    if not await _can_settle_from(session, group, user_id, data.from_member_id):
        # Linked members may only record payments they themselves made.
        raise PermissionError(
            "You can only record settlements where you are the payer"
        )

    await _validate_members_in_group(
        session, group_id, [data.from_member_id, data.to_member_id]
    )
    if (
        data.transaction_id is not None
        and data.transaction_id == data.receiver_transaction_id
    ):
        raise ValueError("The two sides of a contribution must be two transactions")
    await _validate_transaction(session, data.transaction_id, workspace_id)
    await _validate_transaction(session, data.receiver_transaction_id, workspace_id)

    payload = data.model_dump()
    account_id = payload.pop("account_id", None)
    description = payload.pop("description", None)
    create_receiver_transaction = payload.pop("create_receiver_transaction", False)
    if create_receiver_transaction and data.receiver_transaction_id is not None:
        raise ValueError(
            "Pass either receiver_transaction_id (to link an existing "
            "transaction) or create_receiver_transaction, not both"
        )

    # Resolve member metadata once — we need names for descriptions
    # and the to_member's linked_user_id for the receiver-side credit.
    members_q = await session.execute(
        select(
            GroupMember.id,
            GroupMember.name,
            GroupMember.linked_user_id,
            GroupMember.is_self,
        ).where(GroupMember.id.in_([data.from_member_id, data.to_member_id]))
    )
    member_meta = {row.id: row for row in members_q.all()}
    from_name = member_meta[data.from_member_id].name if data.from_member_id in member_meta else "—"
    to_meta = member_meta.get(data.to_member_id)
    to_name = to_meta.name if to_meta else "—"
    # Resolve the receiver's Securo user id. linked_user_id wins; fall
    # back to group.user_id when the receiver is the owner's
    # self-member (owners often don't bother linking themselves).
    receiver_user_id = None
    if to_meta is not None:
        receiver_user_id = to_meta.linked_user_id
        if receiver_user_id is None and to_meta.is_self:
            receiver_user_id = group.user_id

    # Optional integration with the real account ledger: create a debit
    # transaction on the payer's account and link it via transaction_id.
    if account_id is not None:
        if payload.get("transaction_id") is not None:
            raise ValueError(
                "Pass either account_id (to create a transaction) or "
                "transaction_id (to link an existing one), not both"
            )
        auto_desc = description or f"Acerto · {group.name} · {to_name}"
        tx = await _create_payment_transaction(
            session,
            user_id,
            workspace_id,
            account_id,
            data.amount,
            data.currency,
            data.date,
            auto_desc,
        )
        payload["transaction_id"] = tx.id

    # Receiver-side mirror credit. Written only when the caller asks for
    # it: a contribution is the real bank transaction, and inventing a
    # second row for money that already shows in an imported account
    # counts it twice. Still needs the receiver to map to a Securo user
    # with a checking/savings account to land anywhere.
    if create_receiver_transaction and receiver_user_id is not None:
        receiver_desc = description or f"Acerto · {group.name} · {from_name}"
        receiver_tx = await _create_receiver_credit(
            session,
            receiver_user_id,
            data.amount,
            data.currency,
            data.date,
            receiver_desc,
        )
        if receiver_tx is not None:
            payload["receiver_transaction_id"] = receiver_tx.id

    settlement = GroupSettlement(
        group_id=group_id, workspace_id=workspace_id, **payload
    )
    session.add(settlement)
    await session.commit()
    await session.refresh(settlement)
    await attach_links(session, [settlement])
    return settlement


async def update_settlement(
    session: AsyncSession,
    group_id: uuid.UUID,
    settlement_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    data: GroupSettlementUpdate,
) -> Optional[GroupSettlement]:
    group = await _ensure_group_visible(session, group_id, workspace_id, user_id)
    if not group:
        return None

    result = await session.execute(
        select(GroupSettlement).where(
            GroupSettlement.id == settlement_id,
            GroupSettlement.group_id == group_id,
        )
    )
    settlement = result.scalar_one_or_none()
    if not settlement:
        return None

    # Caller must currently own the settlement (linked member of the
    # original from_member, or the group owner).
    if not await _can_settle_from(session, group, user_id, settlement.from_member_id):
        raise PermissionError("You can only edit settlements you created")

    update_data = data.model_dump(exclude_unset=True)

    new_from = update_data.get("from_member_id", settlement.from_member_id)
    new_to = update_data.get("to_member_id", settlement.to_member_id)
    if new_from == new_to:
        raise ValueError("from_member_id and to_member_id must differ")

    member_check: list[uuid.UUID] = []
    if "from_member_id" in update_data:
        member_check.append(update_data["from_member_id"])
    if "to_member_id" in update_data:
        member_check.append(update_data["to_member_id"])
    if member_check:
        await _validate_members_in_group(session, group_id, member_check)

    new_payer_link = update_data.get("transaction_id", settlement.transaction_id)
    new_receiver_link = update_data.get(
        "receiver_transaction_id", settlement.receiver_transaction_id
    )
    if new_payer_link is not None and new_payer_link == new_receiver_link:
        raise ValueError("The two sides of a contribution must be two transactions")

    for column in ("transaction_id", "receiver_transaction_id"):
        if column in update_data:
            await _validate_transaction(
                session, update_data[column], workspace_id, settlement_id=settlement.id
            )

    for key, value in update_data.items():
        setattr(settlement, key, value)

    await session.commit()
    await session.refresh(settlement)
    await attach_links(session, [settlement])
    return settlement


async def delete_settlement(
    session: AsyncSession,
    group_id: uuid.UUID,
    settlement_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    group = await _ensure_group_visible(session, group_id, workspace_id, user_id)
    if not group:
        return False
    result = await session.execute(
        select(GroupSettlement).where(
            GroupSettlement.id == settlement_id,
            GroupSettlement.group_id == group_id,
        )
    )
    settlement = result.scalar_one_or_none()
    if not settlement:
        return False
    if not await _can_settle_from(session, group, user_id, settlement.from_member_id):
        raise PermissionError("You can only delete settlements you created")

    # Only rows this service invented go with it. A linked bank row is
    # the user's record of money that really moved and survives the
    # contribution being deleted.
    synthetic = await _synthetic_linked_transactions(session, settlement)
    await session.delete(settlement)
    await session.flush()
    for tx in synthetic:
        await session.delete(tx)
    await session.commit()
    return True


async def _synthetic_linked_transactions(
    session: AsyncSession, settlement: GroupSettlement
) -> list[Transaction]:
    linked = [
        link
        for link in (settlement.transaction_id, settlement.receiver_transaction_id)
        if link is not None
    ]
    if not linked:
        return []
    result = await session.execute(
        select(Transaction).where(
            Transaction.id.in_(linked),
            Transaction.source == SYNTHETIC_SOURCE,
        )
    )
    return list(result.scalars().all())


async def mark_transaction_as_contribution(
    session: AsyncSession,
    group_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    data: MarkContributionFromTransaction,
) -> Optional[GroupSettlement]:
    """Turn a real bank transaction into a contribution in one step.

    Everything but the counterparty is read off the transaction: the
    amount, the currency, the date it is bucketed by, and which side of
    the contribution it is. A debit is the payer's side — the account's
    owner moved money out — and a credit is the receiver's. The member on
    the other side is the one the caller names, and it has to be a member
    of this group.

    No transaction is created: the one that is already there is the
    contribution.
    """
    from app.services.position_service import _load_identity, _resolve_payer

    group = await _ensure_group_visible(session, group_id, workspace_id, user_id)
    if not group:
        return None

    tx = (
        await session.execute(
            select(Transaction).where(
                Transaction.id == data.transaction_id,
                Transaction.workspace_id == workspace_id,
            )
        )
    ).scalar_one_or_none()
    if tx is None:
        raise ValueError("Linked transaction not found")
    if tx.type not in ("debit", "credit"):
        raise ValueError("Only a debit or a credit can be a contribution")

    identity = await _load_identity(session, group_id, workspace_id, user_id)
    if identity is None:
        return None
    if not any(m.id == data.member_id for m in identity.members):
        raise ValueError("The other member must belong to the group")

    account_owner_id = (
        await session.execute(select(Account.user_id).where(Account.id == tx.account_id))
    ).scalar_one_or_none()
    # The same rule positions use for who paid a shared transaction, so
    # the two never disagree about whose account this is.
    account_member_id, _assumed = _resolve_payer(identity, account_owner_id)
    if account_member_id is None:
        raise ValueError(
            "This group has no member for its owner, so the other side of "
            "the contribution cannot be resolved"
        )
    if account_member_id == data.member_id:
        raise ValueError("The other member must not be the one whose account this is")

    if tx.type == "debit":
        from_member_id, to_member_id = account_member_id, data.member_id
        links = {"transaction_id": tx.id}
    else:
        from_member_id, to_member_id = data.member_id, account_member_id
        links = {"receiver_transaction_id": tx.id}

    if not await _can_settle_from(session, group, user_id, from_member_id):
        raise PermissionError(
            "You can only record settlements where you are the payer"
        )

    await _validate_transaction(session, tx.id, workspace_id)

    from app.services._query_filters import reporting_date_of
    from app.services.admin_service import get_credit_card_accounting_mode

    settlement = GroupSettlement(
        group_id=group_id,
        workspace_id=workspace_id,
        from_member_id=from_member_id,
        to_member_id=to_member_id,
        amount=abs(tx.amount),
        currency=tx.currency,
        date=reporting_date_of(tx, await get_credit_card_accounting_mode(session)),
        notes=data.notes,
        **links,
    )
    session.add(settlement)
    await session.commit()
    await session.refresh(settlement)
    await attach_links(session, [settlement])
    return settlement


async def attach_paired_leg(
    session: AsyncSession, debit: Transaction, credit: Transaction
) -> bool:
    """When a transfer pair turns out to be a contribution already on the
    books, hang the newly imported leg off it instead of leaving it loose.

    Both legs of one bank transfer are one event, so the contribution
    ends up holding the payer's debit and the receiver's credit. A legacy
    row that put the receiver's credit in the payer-side column is
    normalised here — the one place a link column is rewritten, and only
    because a second leg has arrived to take the side it was standing in.

    Returns True when a contribution was extended. Does not commit: the
    caller's transaction owns the change.
    """
    result = await session.execute(
        select(GroupSettlement).where(
            or_(
                GroupSettlement.transaction_id.in_([debit.id, credit.id]),
                GroupSettlement.receiver_transaction_id.in_([debit.id, credit.id]),
            )
        )
    )
    settlements = list(result.scalars().all())
    if len(settlements) != 1:
        # Neither leg is a contribution, or each leg belongs to a
        # different one — which the guard should have prevented and which
        # is not this function's to resolve.
        return False

    settlement = settlements[0]
    legs = {debit.id, credit.id}
    occupied = {
        link
        for link in (settlement.transaction_id, settlement.receiver_transaction_id)
        if link is not None
    }
    if not occupied <= legs:
        # It also links a third transaction — a synthetic mirror, say.
        # Replacing that silently would drop a link nobody asked to drop.
        return False
    if occupied == legs and settlement.transaction_id == debit.id:
        return False

    settlement.transaction_id = debit.id
    settlement.receiver_transaction_id = credit.id
    return True
