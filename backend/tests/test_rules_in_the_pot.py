"""Rules that do the household's bookkeeping.

Two rule actions work on the common pot: sharing a transaction in a group
with a given distribution, and marking one as a contribution from a
member. The household they are written for is two people in one
workspace, both with bank-synced accounts: her transfer to him is her
contribution however many times the rules run and whichever leg the bank
sends first, and the groceries either of them pays are shared 50/50 once.

These tests state bank rows and rules, and assert what the pot is left
with: one contribution, one set of shares, and a transaction taken out of
the pot that stays out.
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import bcrypt
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.group_settlement import GroupSettlement
from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.models.user import User
from app.models.workspace import WorkspaceMember
from app.schemas.group import GroupCreate, GroupMemberCreate
from app.schemas.rule import RuleCreate
from app.schemas.transaction import TransactionCreate, TransactionImport
from app.schemas.transaction_split import (
    TransactionSplitInput,
    TransactionSplitsInput,
)
from app.services import (
    group_service,
    import_service,
    position_service,
    rule_service,
    split_service,
    transaction_service,
)

D = Decimal


# ────────────────────────────── the household ───────────────────────────


async def _partner(session: AsyncSession, workspace) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"partner-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password=bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode(),
        is_active=True,
        is_verified=True,
    )
    session.add(user)
    await session.flush()
    session.add(
        WorkspaceMember(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            user_id=user.id,
            role="editor",
        )
    )
    await session.flush()
    return user


async def _account(session, user_id, workspace_id, name) -> Account:
    account = Account(
        id=uuid.uuid4(),
        user_id=user_id,
        workspace_id=workspace_id,
        name=name,
        type="checking",
        balance=D("0"),
        currency="USD",
    )
    session.add(account)
    await session.flush()
    return account


async def _household(session, test_user, test_workspace) -> dict:
    """Both of them in one workspace, one household group, an account
    each — the shape the whole feature is written for."""
    partner_user = await _partner(session, test_workspace)
    mine = await _account(session, test_user.id, test_workspace.id, "Mine")
    hers = await _account(session, partner_user.id, test_workspace.id, "Hers")

    group = await group_service.create_group(
        session,
        test_workspace.id,
        test_user.id,
        GroupCreate(name="Home", kind="household", default_currency="USD"),
    )
    me = await group_service.create_member(
        session, group.id, test_workspace.id, GroupMemberCreate(name="Me", is_self=True)
    )
    her = await group_service.create_member(
        session,
        group.id,
        test_workspace.id,
        GroupMemberCreate(name="Partner", linked_user_id=partner_user.id),
    )
    await session.commit()
    return {
        "group": group,
        "me": me,
        "her": her,
        "partner_user": partner_user,
        "mine": mine,
        "hers": hers,
    }


def _share_action(home, members=None, share_type="equal", pcts=None) -> dict:
    members = members or [home["me"], home["her"]]
    splits = []
    for i, member in enumerate(members):
        split: dict = {"group_member_id": str(member.id)}
        if share_type == "percent":
            split["share_pct"] = (pcts or [])[i]
        splits.append(split)
    return {
        "op": "share_in_group",
        "value": {
            "group_id": str(home["group"].id),
            "share_type": share_type,
            "splits": splits,
        },
    }


def _contribution_action(home, member=None) -> dict:
    return {
        "op": "mark_as_contribution",
        "value": {
            "group_id": str(home["group"].id),
            # The member on the other side: his credit is her
            # contribution to him.
            "member_id": str((member or home["her"]).id),
        },
    }


async def _rule(session, workspace_id, user_id, name, contains, actions, **kwargs):
    return await rule_service.create_rule(
        session,
        workspace_id,
        user_id,
        RuleCreate(
            name=name,
            conditions_op="and",
            conditions=[{"field": "description", "op": "contains", "value": contains}],
            actions=actions,
            apply_to_existing=False,
            **kwargs,
        ),
    )


async def _bank_row(
    session,
    account: Account,
    amount: str,
    *,
    type_: str = "debit",
    description: str = "SUPERMARKET",
    when: date | None = None,
) -> Transaction:
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=account.user_id,
        workspace_id=account.workspace_id,
        account_id=account.id,
        description=description,
        original_description=description,
        amount=D(amount),
        currency="USD",
        date=when or date(2026, 5, 15),
        effective_date=when or date(2026, 5, 15),
        type=type_,
        source="sync",
        created_at=datetime.now(timezone.utc),
    )
    session.add(tx)
    await session.flush()
    return tx


async def _shares_of(session, transaction_id) -> list[TransactionSplit]:
    rows = await session.execute(
        select(TransactionSplit).where(
            TransactionSplit.transaction_id == transaction_id
        )
    )
    return list(rows.scalars().all())


async def _contributions(session, group_id) -> list[GroupSettlement]:
    rows = await session.execute(
        select(GroupSettlement).where(GroupSettlement.group_id == group_id)
    )
    return list(rows.scalars().all())


# ───────────────────────── the rule definition ──────────────────────────


@pytest.mark.asyncio
async def test_a_sharing_rule_is_stored_with_its_group_and_distribution(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    rule = await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Groceries",
        "SUPERMARKET",
        [_share_action(home)],
    )
    assert rule.actions[0]["op"] == "share_in_group"
    assert rule.actions[0]["value"]["group_id"] == str(home["group"].id)
    assert len(rule.actions[0]["value"]["splits"]) == 2


@pytest.mark.asyncio
async def test_a_rule_naming_a_group_of_another_workspace_is_refused(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    other_workspace = uuid.uuid4()
    with pytest.raises(ValueError, match="Group not found"):
        await _rule(
            session,
            test_workspace.id,
            test_user.id,
            "Groceries",
            "SUPERMARKET",
            [
                {
                    "op": "share_in_group",
                    "value": {
                        "group_id": str(other_workspace),
                        "share_type": "equal",
                        "splits": [{"group_member_id": str(home["me"].id)}],
                    },
                }
            ],
        )


@pytest.mark.asyncio
async def test_a_rule_naming_a_member_of_another_group_is_refused(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    other = await group_service.create_group(
        session, test_workspace.id, test_user.id, GroupCreate(name="Trip")
    )
    stranger = await group_service.create_member(
        session, other.id, test_workspace.id, GroupMemberCreate(name="Someone")
    )
    await session.commit()
    with pytest.raises(ValueError, match="members not found"):
        await _rule(
            session,
            test_workspace.id,
            test_user.id,
            "Groceries",
            "SUPERMARKET",
            [_share_action(home, members=[home["me"], stranger])],
        )


@pytest.mark.asyncio
async def test_a_distribution_that_does_not_add_up_is_refused(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    with pytest.raises(ValueError, match="sum to 100"):
        await _rule(
            session,
            test_workspace.id,
            test_user.id,
            "Groceries",
            "SUPERMARKET",
            [_share_action(home, share_type="percent", pcts=[60, 30])],
        )


@pytest.mark.asyncio
async def test_a_rule_cannot_carry_exact_amounts(session, test_user, test_workspace):
    """A rule fires on transactions of every size, so an amount that fits
    one of them fits no other."""
    home = await _household(session, test_user, test_workspace)
    with pytest.raises(ValueError, match="Invalid group sharing action"):
        await _rule(
            session,
            test_workspace.id,
            test_user.id,
            "Groceries",
            "SUPERMARKET",
            [
                {
                    "op": "share_in_group",
                    "value": {
                        "group_id": str(home["group"].id),
                        "share_type": "exact",
                        "splits": [
                            {"group_member_id": str(home["me"].id), "share_amount": 10}
                        ],
                    },
                }
            ],
        )


@pytest.mark.asyncio
async def test_a_contribution_rule_naming_a_non_member_is_refused(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    with pytest.raises(ValueError, match="members not found"):
        await _rule(
            session,
            test_workspace.id,
            test_user.id,
            "Her transfer",
            "MONTHLY",
            [
                {
                    "op": "mark_as_contribution",
                    "value": {
                        "group_id": str(home["group"].id),
                        "member_id": str(uuid.uuid4()),
                    },
                }
            ],
        )


# ───────────── idempotence, one path at a time (apply twice) ────────────


@pytest.mark.asyncio
async def test_apply_one_rule_twice_leaves_one_set_of_shares(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    groceries = await _bank_row(session, home["hers"], "300.00")
    await session.commit()
    rule = await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Groceries",
        "SUPERMARKET",
        [_share_action(home)],
    )

    first = await rule_service.apply_single_rule(session, test_workspace.id, rule)
    second = await rule_service.apply_single_rule(session, test_workspace.id, rule)

    assert first == 1
    assert second == 0, "a second run has nothing left to do"
    shares = await _shares_of(session, groceries.id)
    assert sorted(s.share_amount for s in shares) == [D("150.00"), D("150.00")]


@pytest.mark.asyncio
async def test_apply_one_rule_twice_leaves_one_contribution(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    credit = await _bank_row(
        session,
        home["mine"],
        "2000.00",
        type_="credit",
        description="MONTHLY TRANSFER",
    )
    await session.commit()
    rule = await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Her transfer",
        "MONTHLY TRANSFER",
        [_contribution_action(home)],
    )

    await rule_service.apply_single_rule(session, test_workspace.id, rule)
    await rule_service.apply_single_rule(session, test_workspace.id, rule)

    rows = await _contributions(session, home["group"].id)
    assert len(rows) == 1
    assert rows[0].receiver_transaction_id == credit.id
    assert rows[0].from_member_id == home["her"].id
    assert rows[0].to_member_id == home["me"].id
    assert rows[0].amount == D("2000.00")


@pytest.mark.asyncio
async def test_apply_all_rules_twice_leaves_one_of_each(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    groceries = await _bank_row(session, home["hers"], "300.00")
    credit = await _bank_row(
        session,
        home["mine"],
        "2000.00",
        type_="credit",
        description="MONTHLY TRANSFER",
    )
    await session.commit()
    await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Groceries",
        "SUPERMARKET",
        [_share_action(home)],
    )
    await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Her transfer",
        "MONTHLY TRANSFER",
        [_contribution_action(home)],
    )

    await rule_service.apply_all_rules(session, test_workspace.id)
    await rule_service.apply_all_rules(session, test_workspace.id)

    assert len(await _shares_of(session, groceries.id)) == 2
    rows = await _contributions(session, home["group"].id)
    assert len(rows) == 1
    assert rows[0].receiver_transaction_id == credit.id


@pytest.mark.asyncio
async def test_a_manual_transaction_without_a_category_is_shared_once(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Groceries",
        "SUPERMARKET",
        [_share_action(home)],
    )

    created = await transaction_service.create_transaction(
        session,
        test_workspace.id,
        test_user.id,
        TransactionCreate(
            account_id=home["mine"].id,
            description="SUPERMARKET",
            amount=D("80.00"),
            date=date(2026, 5, 15),
            type="debit",
        ),
    )
    shares = await _shares_of(session, created.id)
    assert sorted(s.share_amount for s in shares) == [D("40.00"), D("40.00")]

    # And running the rules over it again changes nothing.
    await rule_service.apply_all_rules(session, test_workspace.id)
    shares = await _shares_of(session, created.id)
    assert sorted(s.share_amount for s in shares) == [D("40.00"), D("40.00")]


@pytest.mark.asyncio
async def test_a_manual_credit_without_a_category_is_marked_once(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Her transfer",
        "MONTHLY TRANSFER",
        [_contribution_action(home)],
    )

    created = await transaction_service.create_transaction(
        session,
        test_workspace.id,
        test_user.id,
        TransactionCreate(
            account_id=home["mine"].id,
            description="MONTHLY TRANSFER",
            amount=D("2000.00"),
            date=date(2026, 5, 5),
            type="credit",
        ),
    )
    rows = await _contributions(session, home["group"].id)
    assert len(rows) == 1
    assert rows[0].receiver_transaction_id == created.id

    await rule_service.apply_all_rules(session, test_workspace.id)
    assert len(await _contributions(session, home["group"].id)) == 1


@pytest.mark.asyncio
async def test_an_imported_transaction_is_shared_once(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Groceries",
        "SUPERMARKET",
        [_share_action(home)],
    )

    imported, _skipped, _excluded, _log = await import_service.import_transactions(
        session,
        test_workspace.id,
        test_user.id,
        home["mine"].id,
        [
            TransactionImport(
                description="SUPERMARKET",
                amount=D("300.00"),
                date=date(2026, 5, 15),
                type="debit",
                currency="USD",
                force_uncategorized=True,
            )
        ],
        "csv",
    )
    assert imported == 1
    tx = (
        await session.execute(
            select(Transaction).where(Transaction.description == "SUPERMARKET")
        )
    ).scalar_one()
    assert sorted(s.share_amount for s in await _shares_of(session, tx.id)) == [
        D("150.00"),
        D("150.00"),
    ]

    # The same file imported again is deduplicated into the same row, and
    # the rules that run over it leave the one set of shares alone.
    await import_service.import_transactions(
        session,
        test_workspace.id,
        test_user.id,
        home["mine"].id,
        [
            TransactionImport(
                description="SUPERMARKET",
                amount=D("300.00"),
                date=date(2026, 5, 15),
                type="debit",
                currency="USD",
                force_uncategorized=True,
            )
        ],
        "csv",
    )
    all_shares = (
        await session.execute(
            select(TransactionSplit).join(
                Transaction, Transaction.id == TransactionSplit.transaction_id
            )
        )
    ).scalars().all()
    assert len(all_shares) == 2


@pytest.mark.asyncio
async def test_an_imported_credit_becomes_one_contribution(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Her transfer",
        "MONTHLY TRANSFER",
        [_contribution_action(home)],
    )

    for _ in range(2):
        await import_service.import_transactions(
            session,
            test_workspace.id,
            test_user.id,
            home["mine"].id,
            [
                TransactionImport(
                    description="MONTHLY TRANSFER",
                    amount=D("2000.00"),
                    date=date(2026, 5, 5),
                    type="credit",
                    currency="USD",
                    force_uncategorized=True,
                )
            ],
            "csv",
        )

    assert len(await _contributions(session, home["group"].id)) == 1


# ───────────────────────────── the bank sync ────────────────────────────


def _provider(transactions, account_ext="acc-ext-1", currency="USD"):
    from app.providers.base import AccountData

    provider = AsyncMock()
    provider.refresh_credentials = AsyncMock(return_value={"token": "t"})
    provider.get_accounts = AsyncMock(
        return_value=[
            AccountData(
                external_id=account_ext,
                name="Checking",
                type="checking",
                balance=D("0"),
                currency=currency,
            )
        ]
    )
    provider.get_transactions = AsyncMock(return_value=transactions)
    return provider


def _incoming(**kw):
    from app.providers.base import TransactionData

    kw.setdefault("currency", "USD")
    kw.setdefault("type", "debit")
    kw.setdefault("status", "posted")
    return TransactionData(**kw)


async def _connected_account(session, user_id, workspace_id, account: Account, ext):
    """Hang an existing account off a bank connection, so a sync lands on
    the member's own account and the payer is really them."""
    conn = BankConnection(
        id=uuid.uuid4(),
        user_id=user_id,
        provider="test",
        external_id=f"ext-{uuid.uuid4().hex[:8]}",
        institution_name="Bank",
        credentials={"token": "fake"},
        status="active",
        last_sync_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    session.add(conn)
    account.connection_id = conn.id
    account.external_id = ext
    await session.commit()
    return conn


async def _sync(session, conn_id, workspace_id, user_id, provider):
    from app.services.connection_service import sync_connection

    with patch(
        "app.services.connection_service.get_provider", return_value=provider
    ), patch(
        "app.services.connection_service.stamp_primary_amount", new_callable=AsyncMock
    ):
        return await sync_connection(session, conn_id, workspace_id, user_id)


@pytest.mark.asyncio
async def test_a_bank_sync_shares_groceries_once_however_often_it_runs(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    conn = await _connected_account(
        session,
        home["partner_user"].id,
        test_workspace.id,
        home["hers"],
        "acc-hers",
    )
    await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Groceries",
        "SUPERMARKET",
        [_share_action(home)],
    )
    provider = _provider(
        [
            _incoming(
                external_id="s1",
                description="SUPERMARKET",
                amount=D("300.00"),
                date=date(2026, 5, 15),
            )
        ],
        account_ext="acc-hers",
    )

    await _sync(session, conn.id, test_workspace.id, home["partner_user"].id, provider)
    await _sync(session, conn.id, test_workspace.id, home["partner_user"].id, provider)

    tx = (
        await session.execute(
            select(Transaction).where(Transaction.external_id == "s1")
        )
    ).scalar_one()
    shares = await _shares_of(session, tx.id)
    assert sorted(s.share_amount for s in shares) == [D("150.00"), D("150.00")]


@pytest.mark.asyncio
async def test_a_bank_sync_makes_her_transfer_one_contribution(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    conn = await _connected_account(
        session, test_user.id, test_workspace.id, home["mine"], "acc-mine"
    )
    await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Her transfer",
        "MONTHLY TRANSFER",
        [_contribution_action(home)],
    )
    provider = _provider(
        [
            _incoming(
                external_id="c1",
                description="MONTHLY TRANSFER",
                amount=D("2000.00"),
                date=date(2026, 5, 5),
                type="credit",
            )
        ],
        account_ext="acc-mine",
    )

    await _sync(session, conn.id, test_workspace.id, test_user.id, provider)
    await _sync(session, conn.id, test_workspace.id, test_user.id, provider)

    rows = await _contributions(session, home["group"].id)
    assert len(rows) == 1
    assert rows[0].amount == D("2000.00")


@pytest.mark.asyncio
async def test_both_legs_of_her_transfer_make_one_contribution_either_way_round(
    session, test_user, test_workspace
):
    """Both accounts are imported, so the bank sends her debit and his
    credit. Whichever arrives first, the household ends up with one
    contribution holding both legs."""
    home = await _household(session, test_user, test_workspace)
    his_conn = await _connected_account(
        session, test_user.id, test_workspace.id, home["mine"], "acc-mine"
    )
    her_conn = await _connected_account(
        session,
        home["partner_user"].id,
        test_workspace.id,
        home["hers"],
        "acc-hers",
    )
    # His credit names her as the other side; her debit names him.
    await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Transfer in",
        "MONTHLY TRANSFER",
        [_contribution_action(home, member=home["her"])],
    )
    await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Transfer out",
        "MONTHLY TRANSFER",
        [_contribution_action(home, member=home["me"])],
    )

    await _sync(
        session,
        her_conn.id,
        test_workspace.id,
        home["partner_user"].id,
        _provider(
            [
                _incoming(
                    external_id="d1",
                    description="MONTHLY TRANSFER",
                    amount=D("2000.00"),
                    date=date(2026, 5, 5),
                )
            ],
            account_ext="acc-hers",
        ),
    )
    await _sync(
        session,
        his_conn.id,
        test_workspace.id,
        test_user.id,
        _provider(
            [
                _incoming(
                    external_id="c1",
                    description="MONTHLY TRANSFER",
                    amount=D("2000.00"),
                    date=date(2026, 5, 5),
                    type="credit",
                )
            ],
            account_ext="acc-mine",
        ),
    )

    rows = await _contributions(session, home["group"].id)
    assert len(rows) == 1, "one transfer, one contribution"
    debit = (
        await session.execute(select(Transaction).where(Transaction.external_id == "d1"))
    ).scalar_one()
    credit = (
        await session.execute(select(Transaction).where(Transaction.external_id == "c1"))
    ).scalar_one()
    assert rows[0].transaction_id == debit.id
    assert rows[0].receiver_transaction_id == credit.id

    positions = await position_service.compute_positions(
        session, home["group"].id, test_workspace.id, test_user.id
    )
    assert positions is not None
    made = {p.member_id: p.contributions_made for p in positions.positions}
    assert made[home["her"].id] == D("2000.00"), "counted once, not twice"


# ───────────────── placeholder reconciliation carries them ──────────────


async def _recurring_placeholder(session, workspace_id, user_id, account, description):
    """A generated placeholder waiting for the charge that fulfils it."""
    from app.schemas.recurring_transaction import RecurringTransactionCreate
    from app.services.recurring_transaction_service import (
        create_recurring_transaction,
        generate_pending,
    )

    await create_recurring_transaction(
        session,
        workspace_id,
        user_id,
        RecurringTransactionCreate(
            description=description,
            amount=D("300.00"),
            currency="USD",
            type="debit",
            frequency="monthly",
            start_date=date(2026, 5, 15),
            account_id=account.id,
        ),
    )
    await generate_pending(session, user_id, up_to=date(2026, 5, 20))
    await session.commit()
    row = (
        await session.execute(
            select(Transaction).where(
                Transaction.account_id == account.id,
                Transaction.source == "recurring",
            )
        )
    ).scalars().first()
    assert row is not None
    return row


@pytest.mark.asyncio
async def test_a_synced_charge_that_upgrades_a_placeholder_still_shares_it(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    conn = await _connected_account(
        session, test_user.id, test_workspace.id, home["mine"], "acc-mine"
    )
    placeholder = await _recurring_placeholder(
        session, test_workspace.id, test_user.id, home["mine"], "SUPERMARKET"
    )
    await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Groceries",
        "SUPERMARKET",
        [_share_action(home)],
    )
    # The sync commits, which expires everything loaded before it.
    account_id, placeholder_id = home["mine"].id, placeholder.id

    await _sync(
        session,
        conn.id,
        test_workspace.id,
        test_user.id,
        _provider(
            [
                _incoming(
                    external_id="s1",
                    description="SUPERMARKET",
                    amount=D("300.00"),
                    date=date(2026, 5, 15),
                )
            ],
            account_ext="acc-mine",
        ),
    )

    rows = (
        await session.execute(
            select(Transaction).where(Transaction.account_id == account_id)
        )
    ).scalars().all()
    surviving = [r for r in rows if r.source != "opening_balance"]
    assert len(surviving) == 1, "the placeholder was upgraded, not doubled"
    assert surviving[0].id == placeholder_id
    assert surviving[0].external_id == "s1", "the charge's provenance is on it"
    shares = await _shares_of(session, placeholder_id)
    assert sorted(s.share_amount for s in shares) == [D("150.00"), D("150.00")]


@pytest.mark.asyncio
async def test_an_imported_charge_that_upgrades_a_placeholder_still_shares_it(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    placeholder = await _recurring_placeholder(
        session, test_workspace.id, test_user.id, home["mine"], "SUPERMARKET"
    )
    await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Groceries",
        "SUPERMARKET",
        [_share_action(home)],
    )

    await import_service.import_transactions(
        session,
        test_workspace.id,
        test_user.id,
        home["mine"].id,
        [
            TransactionImport(
                description="SUPERMARKET",
                amount=D("300.00"),
                date=date(2026, 5, 15),
                type="debit",
                currency="USD",
                force_uncategorized=True,
            )
        ],
        "csv",
        # Duplicate detection would file the charge as a repeat of the
        # placeholder and never reach the reconciliation this tests.
        detect_duplicates=False,
    )

    rows = (
        await session.execute(
            select(Transaction).where(Transaction.account_id == home["mine"].id)
        )
    ).scalars().all()
    assert len(rows) == 1, "the placeholder was upgraded, not doubled"
    assert rows[0].id == placeholder.id
    assert rows[0].source == "csv", "the charge's provenance is on it"
    shares = await _shares_of(session, placeholder.id)
    assert sorted(s.share_amount for s in shares) == [D("150.00"), D("150.00")]


# ─────────────── what a rule must never touch, and the pot ──────────────


@pytest.mark.asyncio
async def test_a_distribution_set_by_hand_survives_both_apply_paths(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    groceries = await _bank_row(session, home["mine"], "300.00")
    await split_service.replace_splits(
        session,
        groceries,
        TransactionSplitsInput(
            share_type="percent",
            splits=[
                TransactionSplitInput(group_member_id=home["me"].id, share_pct=D("80")),
                TransactionSplitInput(group_member_id=home["her"].id, share_pct=D("20")),
            ],
        ),
        test_user.id,
    )
    await session.commit()
    rule = await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Groceries",
        "SUPERMARKET",
        [_share_action(home)],
    )

    await rule_service.apply_single_rule(session, test_workspace.id, rule)
    await rule_service.apply_all_rules(session, test_workspace.id)

    shares = {s.group_member_id: s.share_amount for s in await _shares_of(session, groceries.id)}
    assert shares == {home["me"].id: D("240.00"), home["her"].id: D("60.00")}


@pytest.mark.asyncio
async def test_a_transaction_taken_out_of_the_pot_stays_out(
    session, test_user, test_workspace
):
    """Out of the pot is 100 % on the payer: it moves no position, and a
    rule passes over it because it already carries shares."""
    home = await _household(session, test_user, test_workspace)
    personal = await _bank_row(session, home["mine"], "300.00")
    await split_service.replace_splits(
        session,
        personal,
        TransactionSplitsInput(
            share_type="percent",
            splits=[
                TransactionSplitInput(group_member_id=home["me"].id, share_pct=D("100"))
            ],
        ),
        test_user.id,
    )
    await session.commit()
    rule = await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Groceries",
        "SUPERMARKET",
        [_share_action(home)],
    )

    await rule_service.apply_single_rule(session, test_workspace.id, rule)
    await rule_service.apply_all_rules(session, test_workspace.id)

    shares = await _shares_of(session, personal.id)
    assert [(s.group_member_id, s.share_amount) for s in shares] == [
        (home["me"].id, D("300.00"))
    ]

    positions = await position_service.compute_positions(
        session, home["group"].id, test_workspace.id, test_user.id
    )
    assert positions is not None
    assert all(p.period_position == 0 for p in positions.positions)
    assert all(p.running_position == 0 for p in positions.positions)


@pytest.mark.asyncio
async def test_a_contribution_is_never_shared_by_a_rule(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    credit = await _bank_row(
        session,
        home["mine"],
        "2000.00",
        type_="credit",
        description="MONTHLY TRANSFER",
    )
    await session.commit()
    mark = await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Her transfer",
        "MONTHLY TRANSFER",
        [_contribution_action(home)],
    )
    share = await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Everything",
        "MONTHLY",
        [_share_action(home)],
    )

    await rule_service.apply_single_rule(session, test_workspace.id, mark)
    await rule_service.apply_single_rule(session, test_workspace.id, share)

    assert await _shares_of(session, credit.id) == []
    assert len(await _contributions(session, home["group"].id)) == 1


# ────────────────────── a failing effect rolls back ─────────────────────


@pytest.mark.asyncio
async def test_a_failing_effect_rolls_back_the_rules_other_changes(
    session, test_user, test_workspace, test_categories
):
    """The group the rule shares in is gone, so the rule cannot do what it
    says. Its category is not applied either — half a rule is worse than
    none — and everything else in the workspace is untouched."""
    home = await _household(session, test_user, test_workspace)
    groceries = await _bank_row(session, home["mine"], "300.00")
    await session.commit()
    rule = await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Groceries",
        "SUPERMARKET",
        [
            {"op": "set_category", "value": str(test_categories[0].id)},
            _share_action(home),
        ],
    )
    await group_service.delete_group(session, home["group"].id, test_workspace.id)
    await session.commit()

    count = await rule_service.apply_single_rule(session, test_workspace.id, rule)

    await session.refresh(groceries)
    assert count == 0
    assert groceries.category_id is None
    assert await _shares_of(session, groceries.id) == []


@pytest.mark.asyncio
async def test_a_failing_effect_does_not_stop_a_bank_sync(
    session, test_user, test_workspace, test_categories
):
    """One awkward transaction must not abort the whole sync: the rest of
    the batch lands, and the row whose effect failed is still imported."""
    home = await _household(session, test_user, test_workspace)
    conn = await _connected_account(
        session, test_user.id, test_workspace.id, home["mine"], "acc-mine"
    )
    await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Groceries",
        "SUPERMARKET",
        [
            {"op": "set_category", "value": str(test_categories[0].id)},
            _share_action(home),
        ],
    )
    await group_service.delete_group(session, home["group"].id, test_workspace.id)
    await session.commit()

    await _sync(
        session,
        conn.id,
        test_workspace.id,
        test_user.id,
        _provider(
            [
                _incoming(
                    external_id="s1",
                    description="SUPERMARKET",
                    amount=D("300.00"),
                    date=date(2026, 5, 15),
                ),
                _incoming(
                    external_id="s2",
                    description="BOOKSHOP",
                    amount=D("40.00"),
                    date=date(2026, 5, 16),
                ),
            ],
            account_ext="acc-mine",
        ),
    )

    rows = {
        row.external_id: row
        for row in (
            await session.execute(
                select(Transaction).where(Transaction.external_id.in_(["s1", "s2"]))
            )
        ).scalars().all()
    }
    assert set(rows) == {"s1", "s2"}, "the sync carried on"
    assert rows["s1"].category_id is None, "its rule changes went back"
    assert await _shares_of(session, rows["s1"].id) == []


@pytest.mark.asyncio
async def test_a_refused_marking_is_skipped_and_the_sync_carries_on(
    session, test_user, test_workspace, test_categories
):
    """A contribution the pot cannot accept — here the rule names the
    member whose own account the money is on — is a fact about that one
    bank row, not a broken rule. It is skipped: the rule's other actions
    stand, the sync finishes, and no contribution is written."""
    home = await _household(session, test_user, test_workspace)
    conn = await _connected_account(
        session, test_user.id, test_workspace.id, home["mine"], "acc-mine"
    )
    await _rule(
        session,
        test_workspace.id,
        test_user.id,
        "Her transfer",
        "MONTHLY TRANSFER",
        [
            {"op": "set_category", "value": str(test_categories[0].id)},
            # The credit lands on his account, so naming him as the
            # other side is exactly the marking the service refuses.
            _contribution_action(home, member=home["me"]),
        ],
    )
    group_id, category_id = home["group"].id, test_categories[0].id

    await _sync(
        session,
        conn.id,
        test_workspace.id,
        test_user.id,
        _provider(
            [
                _incoming(
                    external_id="c1",
                    description="MONTHLY TRANSFER",
                    amount=D("2000.00"),
                    date=date(2026, 5, 5),
                    type="credit",
                ),
                _incoming(
                    external_id="s2",
                    description="BOOKSHOP",
                    amount=D("40.00"),
                    date=date(2026, 5, 6),
                ),
            ],
            account_ext="acc-mine",
        ),
    )

    rows = {
        row.external_id: row
        for row in (
            await session.execute(
                select(Transaction).where(Transaction.external_id.in_(["c1", "s2"]))
            )
        ).scalars().all()
    }
    assert set(rows) == {"c1", "s2"}, "the sync carried on"
    assert rows["c1"].category_id == category_id, "a skip is not a rollback"
    assert await _contributions(session, group_id) == []

# ────────────────────────────── the preview ─────────────────────────────


@pytest.mark.asyncio
async def test_the_preview_counts_the_shares_and_writes_nothing(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    groceries = await _bank_row(session, home["mine"], "300.00")
    await session.commit()

    preview = await rule_service.preview_rule(
        session,
        test_workspace.id,
        "and",
        [{"field": "description", "op": "contains", "value": "SUPERMARKET"}],
        [_share_action(home)],
        user_id=test_user.id,
    )

    assert preview.matched == 1
    assert preview.will_share == 1
    assert preview.will_mark_contribution == 0
    (item,) = preview.sample
    assert item.planned_share is not None
    assert item.planned_share.group_name == "Home"
    assert sorted(line.amount for line in item.planned_share.shares) == [150.0, 150.0]
    assert await _shares_of(session, groceries.id) == []


@pytest.mark.asyncio
async def test_the_preview_shows_the_contribution_it_would_make(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    await _bank_row(
        session,
        home["mine"],
        "2000.00",
        type_="credit",
        description="MONTHLY TRANSFER",
    )
    await session.commit()

    preview = await rule_service.preview_rule(
        session,
        test_workspace.id,
        "and",
        [{"field": "description", "op": "contains", "value": "MONTHLY"}],
        [_contribution_action(home)],
        user_id=test_user.id,
    )

    assert preview.will_mark_contribution == 1
    (item,) = preview.sample
    plan = item.planned_contribution
    assert plan is not None
    assert plan.from_member_name == "Partner"
    assert plan.to_member_name == "Me"
    assert plan.amount == 2000.0
    assert plan.side == "receiver"
    assert plan.outcome == "create"
    assert await _contributions(session, home["group"].id) == []


@pytest.mark.asyncio
async def test_the_preview_says_what_it_would_pass_over(
    session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    personal = await _bank_row(session, home["mine"], "300.00")
    await split_service.replace_splits(
        session,
        personal,
        TransactionSplitsInput(
            share_type="percent",
            splits=[
                TransactionSplitInput(group_member_id=home["me"].id, share_pct=D("100"))
            ],
        ),
        test_user.id,
    )
    await session.commit()

    preview = await rule_service.preview_rule(
        session,
        test_workspace.id,
        "and",
        [{"field": "description", "op": "contains", "value": "SUPERMARKET"}],
        [_share_action(home)],
        user_id=test_user.id,
    )

    assert preview.matched == 1
    assert preview.will_share == 0, "it is out of the pot and stays out"
    (item,) = preview.sample
    assert item.planned_share is None
    assert item.skipped_effects == ["This transaction already carries shares"]


# ──────────────────────────── over the API ──────────────────────────────


@pytest.mark.asyncio
async def test_the_editor_can_save_a_sharing_rule_and_see_what_it_would_do(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    groceries = await _bank_row(session, home["mine"], "300.00")
    await session.commit()
    action = _share_action(home)
    conditions = [{"field": "description", "op": "contains", "value": "SUPERMARKET"}]

    preview = await client.post(
        "/api/rules/preview",
        headers=auth_headers,
        json={"conditions_op": "and", "conditions": conditions, "actions": [action]},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["matched"] == 1
    assert body["will_share"] == 1
    assert body["will_mark_contribution"] == 0
    assert body["sample"][0]["planned_share"]["group_name"] == "Home"
    assert await _shares_of(session, groceries.id) == [], "a preview writes nothing"

    created = await client.post(
        "/api/rules",
        headers=auth_headers,
        json={
            "name": "Groceries",
            "conditions_op": "and",
            "conditions": conditions,
            "actions": [action],
            "apply_to_existing": True,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["applied_count"] == 1
    shares = await _shares_of(session, groceries.id)
    assert sorted(s.share_amount for s in shares) == [D("150.00"), D("150.00")]


@pytest.mark.asyncio
async def test_the_api_refuses_a_rule_that_shares_in_a_group_it_cannot_see(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    response = await client.post(
        "/api/rules",
        headers=auth_headers,
        json={
            "name": "Groceries",
            "conditions_op": "and",
            "conditions": [
                {"field": "description", "op": "contains", "value": "SUPERMARKET"}
            ],
            "actions": [
                {
                    "op": "share_in_group",
                    "value": {
                        "group_id": str(uuid.uuid4()),
                        "share_type": "equal",
                        "splits": [{"group_member_id": str(home["me"].id)}],
                    },
                }
            ],
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Group not found"
