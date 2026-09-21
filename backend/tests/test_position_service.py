"""Positions in a group's common pot: money facts in, money outcomes out.

A position is shares - paid - contributions made + contributions
received; a debit counts positive and a credit negative.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Optional

import bcrypt
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.group import GroupMember
from app.models.transaction import Transaction
from app.models.user import User
from app.models.workspace import WorkspaceMember
from app.schemas.group import GroupCreate, GroupMemberCreate
from app.schemas.group_settlement import GroupSettlementCreate
from app.schemas.transaction import TransactionUpdate
from app.schemas.transaction_split import (
    TransactionSplitInput,
    TransactionSplitsInput,
)
from app.services import (
    balance_service,
    group_service,
    position_service,
    settlement_service,
    split_service,
    transaction_service,
    workspace_service,
)

D = Decimal


async def _make_user(session: AsyncSession, email: str, workspace_id=None) -> User:
    """A user; a member of `workspace_id` when given, otherwise with a
    personal workspace of their own."""
    user = User(
        id=uuid.uuid4(),
        email=email,
        hashed_password=bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode(),
        is_active=True,
        is_superuser=False,
        is_verified=True,
    )
    session.add(user)
    await session.flush()
    if workspace_id is not None:
        session.add(
            WorkspaceMember(
                id=uuid.uuid4(), workspace_id=workspace_id, user_id=user.id, role="editor"
            )
        )
        await session.commit()
    return user


async def _make_account(session, user_id, workspace_id, currency="USD") -> Account:
    account = Account(
        id=uuid.uuid4(),
        user_id=user_id,
        workspace_id=workspace_id,
        name="Account",
        type="checking",
        balance=D("0"),
        currency=currency,
    )
    session.add(account)
    await session.flush()
    return account


async def _make_tx(
    session, account: Account, amount, type_="debit", when: Optional[date] = None, currency=None
) -> Transaction:
    when = when or date(2026, 5, 15)
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=account.user_id,
        workspace_id=account.workspace_id,
        account_id=account.id,
        description="shared",
        amount=D(amount),
        currency=currency or account.currency,
        date=when,
        effective_date=when,
        type=type_,
        source="manual",
    )
    session.add(tx)
    await session.flush()
    return tx


async def _share(session, tx, user_id, members, share_type="equal", values=()):
    splits = []
    for i, m in enumerate(members):
        if share_type == "percent":
            split = TransactionSplitInput(group_member_id=m.id, share_pct=D(values[i]))
        elif share_type == "exact":
            split = TransactionSplitInput(group_member_id=m.id, share_amount=D(values[i]))
        else:
            split = TransactionSplitInput(group_member_id=m.id)
        splits.append(split)
    await split_service.replace_splits(
        session, tx, TransactionSplitsInput(share_type=share_type, splits=splits), user_id
    )
    await session.commit()


async def _contribute(session, group, workspace_id, user_id, from_m, to_m, amount, when=None, currency="USD"):
    await settlement_service.create_settlement(
        session,
        group.id,
        workspace_id,
        user_id,
        GroupSettlementCreate(
            from_member_id=from_m.id,
            to_member_id=to_m.id,
            amount=D(amount),
            currency=currency,
            date=when or date(2026, 5, 20),
        ),
    )
    await session.commit()


async def _member(session, group, workspace_id, **fields) -> GroupMember:
    member = await group_service.create_member(
        session, group.id, workspace_id, GroupMemberCreate(**fields)
    )
    assert member is not None
    return member


async def _household(session, owner, workspace_id, partner_email: Optional[str] = "partner@example.com"):
    """A group with the owner's member and a partner. The partner is
    linked to a user in the same workspace unless `partner_email` is None."""
    partner = None
    if partner_email:
        partner = await _make_user(session, partner_email, workspace_id)
    group = await group_service.create_group(
        session, workspace_id, owner.id, GroupCreate(name="Home", kind="household")
    )
    me = await _member(session, group, workspace_id, name="Me", is_self=True)
    her = await _member(session, group, workspace_id, name="Partner", email=partner_email)
    assert me is not None and her is not None
    return group, me, her, partner


async def _positions(session, group, workspace_id, user_id, start=None, end=None):
    result = await position_service.compute_positions(
        session, group.id, workspace_id, user_id, start=start, end=end
    )
    assert result is not None
    return result


def _by_member(result, field="period_position", currency="USD"):
    return {
        p.member_id: getattr(p, field) for p in result.positions if p.currency == currency
    }


def _assert_sums_to_zero(result):
    for field in ("period_position", "running_position", "backlog"):
        totals: dict[str, Decimal] = {}
        for p in result.positions:
            totals[p.currency] = totals.get(p.currency, D("0")) + getattr(p, field)
        assert all(v == 0 for v in totals.values()), (field, totals)


# ----------------------------------------------------------------- payer and sign


@pytest.mark.asyncio
async def test_household_kind_is_accepted(session, test_user, test_workspace):
    group, *_ = await _household(session, test_user, test_workspace.id)
    assert group.kind == "household"


@pytest.mark.asyncio
async def test_owner_paid_debit_matches_the_owner_ledger(session, test_user, test_workspace):
    group, me, her, _ = await _household(session, test_user, test_workspace.id)
    account = await _make_account(session, test_user.id, test_workspace.id)
    tx = await _make_tx(session, account, "120.00")
    await _share(session, tx, test_user.id, [me, her], "exact", ["80.00", "40.00"])

    result = await _positions(session, group, test_workspace.id, test_user.id)
    assert _by_member(result) == {me.id: D("-40.00"), her.id: D("40.00")}
    assert _by_member(result, "paid") == {me.id: D("120.00"), her.id: D("0")}
    assert _by_member(result, "share") == {me.id: D("80.00"), her.id: D("40.00")}

    balances = await balance_service.compute_balances(
        session, group.id, test_workspace.id, test_user.id
    )
    assert balances is not None
    assert [(ln["member_id"], ln["amount"]) for ln in balances["lines"]] == [
        (her.id, D("40.00"))
    ]


@pytest.mark.asyncio
async def test_non_owner_payer_is_owed_the_others_shares(session, test_user, test_workspace):
    group, me, her, partner = await _household(session, test_user, test_workspace.id)
    her_account = await _make_account(session, partner.id, test_workspace.id)
    tx = await _make_tx(session, her_account, "1000.00")
    await _share(session, tx, test_user.id, [me, her])

    result = await _positions(session, group, test_workspace.id, test_user.id)
    assert _by_member(result) == {me.id: D("500.00"), her.id: D("-500.00")}
    assert result.transfers_period[0].from_member_id == me.id
    assert result.transfers_period[0].to_member_id == her.id
    assert result.transfers_period[0].amount == D("500.00")
    _assert_sums_to_zero(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "share_type,values,expected_partner",
    [
        ("equal", None, "-2000.00"),
        ("percent", ["60", "40"], "-1600.00"),
        ("exact", ["2500.00", "1500.00"], "-1500.00"),
    ],
)
async def test_shared_income_lowers_what_the_other_member_carries(
    session, test_user, test_workspace, share_type, values, expected_partner
):
    group, me, her, _ = await _household(session, test_user, test_workspace.id)
    account = await _make_account(session, test_user.id, test_workspace.id)
    rent = await _make_tx(session, account, "4000.00", type_="credit")
    await _share(session, rent, test_user.id, [me, her], share_type, values)

    result = await _positions(session, group, test_workspace.id, test_user.id)
    by_member = _by_member(result)
    assert by_member[her.id] == D(expected_partner)
    assert by_member[me.id] == -D(expected_partner)
    _assert_sums_to_zero(result)


@pytest.mark.asyncio
async def test_purchase_and_refund_shared_alike_cancel(session, test_user, test_workspace):
    group, me, her, _ = await _household(session, test_user, test_workspace.id)
    account = await _make_account(session, test_user.id, test_workspace.id)
    for type_ in ("debit", "credit"):
        tx = await _make_tx(session, account, "1000.00", type_=type_)
        await _share(session, tx, test_user.id, [me, her])

    result = await _positions(session, group, test_workspace.id, test_user.id)
    assert _by_member(result) == {me.id: D("0"), her.id: D("0")}
    assert result.transfers_period == []
    balances = await balance_service.compute_balances(
        session, group.id, test_workspace.id, test_user.id
    )
    assert balances is not None and balances["lines"] == []


@pytest.mark.asyncio
async def test_contribution_between_two_non_owners_moves_both(session, test_user, test_workspace):
    group, me, her, _ = await _household(session, test_user, test_workspace.id)
    third = await _member(session, group, test_workspace.id, name="Third")
    await _contribute(session, group, test_workspace.id, test_user.id, her, third, "100.00")

    result = await _positions(session, group, test_workspace.id, test_user.id)
    assert _by_member(result) == {me.id: D("0"), her.id: D("-100.00"), third.id: D("100.00")}
    assert _by_member(result, "contributions_made")[her.id] == D("100.00")
    assert _by_member(result, "contributions_received")[third.id] == D("100.00")
    _assert_sums_to_zero(result)


@pytest.mark.asyncio
async def test_positions_sum_to_zero_after_amount_edit(session, test_user, test_workspace):
    group, me, her, partner = await _household(session, test_user, test_workspace.id)
    account = await _make_account(session, test_user.id, test_workspace.id)
    her_account = await _make_account(session, partner.id, test_workspace.id)
    tx = await _make_tx(session, account, "100.00")
    await _share(session, tx, test_user.id, [me, her])
    other = await _make_tx(session, her_account, "30.00")
    await _share(session, other, test_user.id, [me, her])

    # The app lets the amount change under the stored shares.
    updated = await transaction_service.update_transaction(
        session, tx.id, test_workspace.id, test_user.id, TransactionUpdate(amount=D("170.00"))
    )
    assert updated is not None and updated.amount == D("170.00")

    result = await _positions(session, group, test_workspace.id, test_user.id)
    _assert_sums_to_zero(result)
    # The payer is credited with the shares, not the edited amount.
    assert _by_member(result, "paid") == {me.id: D("100.00"), her.id: D("30.00")}
    assert _by_member(result) == {me.id: D("-35.00"), her.id: D("35.00")}


# ----------------------------------------------------------------- periods


@pytest.mark.asyncio
async def test_period_boundaries_running_position_and_backlog(session, test_user, test_workspace):
    group, me, her, _ = await _household(session, test_user, test_workspace.id)
    account = await _make_account(session, test_user.id, test_workspace.id)
    for amount, when in (
        ("50.00", date(2026, 3, 10)),
        ("100.00", date(2026, 4, 30)),
        ("200.00", date(2026, 5, 1)),
    ):
        tx = await _make_tx(session, account, amount, when=when)
        await _share(session, tx, test_user.id, [me, her])
    await _contribute(
        session, group, test_workspace.id, test_user.id, her, me, "20.00", when=date(2026, 4, 30)
    )
    await _contribute(
        session, group, test_workspace.id, test_user.id, her, me, "30.00", when=date(2026, 5, 1)
    )

    ws, uid = test_workspace.id, test_user.id
    march = await _positions(session, group, ws, uid, date(2026, 3, 1), date(2026, 4, 1))
    april = await _positions(session, group, ws, uid, date(2026, 4, 1), date(2026, 5, 1))
    may = await _positions(session, group, ws, uid, date(2026, 5, 1), date(2026, 6, 1))

    # The last day of April and the first of May fall in different periods.
    assert _by_member(april)[her.id] == D("30.00")  # 50 share - 20 contributed
    assert _by_member(may)[her.id] == D("70.00")  # 100 share - 30 contributed

    # Backlog is the running position before the period.
    assert _by_member(april, "backlog")[her.id] == D("25.00")
    assert _by_member(may, "backlog")[her.id] == _by_member(april, "running_position")[her.id]

    # Running position is the sum of the period positions.
    assert _by_member(may, "running_position")[her.id] == (
        _by_member(march)[her.id] + _by_member(april)[her.id] + _by_member(may)[her.id]
    )
    all_time = await _positions(session, group, ws, uid)
    assert _by_member(all_time, "running_position")[her.id] == D("125.00")
    assert _by_member(all_time, "backlog")[her.id] == D("0")
    for result in (march, april, may, all_time):
        _assert_sums_to_zero(result)

    # The kept balances contract takes the same optional dates.
    balances = await balance_service.compute_balances(
        session, group.id, ws, uid, start=date(2026, 5, 1), end=date(2026, 6, 1)
    )
    assert balances is not None
    assert [ln["amount"] for ln in balances["lines"]] == [D("70.00")]

    # Transfers for the period alone and including the backlog.
    assert [t.amount for t in may.transfers_period] == [D("70.00")]
    assert [t.amount for t in may.transfers_running] == [D("125.00")]


@pytest.mark.asyncio
async def test_period_follows_the_reporting_date(session, test_user, test_workspace):
    """A card purchase whose bill was moved by hand belongs to the period
    of that bill, as in budgets and reports."""
    group, me, her, _ = await _household(session, test_user, test_workspace.id)
    account = await _make_account(session, test_user.id, test_workspace.id)
    tx = await _make_tx(session, account, "100.00", when=date(2026, 4, 28))
    tx.effective_bill_date = date(2026, 5, 10)
    await _share(session, tx, test_user.id, [me, her])

    ws, uid = test_workspace.id, test_user.id
    april = await _positions(session, group, ws, uid, date(2026, 4, 1), date(2026, 5, 1))
    may = await _positions(session, group, ws, uid, date(2026, 5, 1), date(2026, 6, 1))
    assert _by_member(april)[her.id] == D("0")
    assert _by_member(may)[her.id] == D("50.00")


# ----------------------------------------------------------------- currencies


@pytest.mark.asyncio
async def test_two_currencies_keep_positions_and_transfers_apart(
    session, test_user, test_workspace
):
    group, me, her, partner = await _household(session, test_user, test_workspace.id)
    usd = await _make_account(session, test_user.id, test_workspace.id, "USD")
    eur = await _make_account(session, partner.id, test_workspace.id, "EUR")
    tx_usd = await _make_tx(session, usd, "60.00")
    tx_eur = await _make_tx(session, eur, "40.00")
    for tx in (tx_usd, tx_eur):
        await _share(session, tx, test_user.id, [me, her])

    result = await _positions(session, group, test_workspace.id, test_user.id)
    assert _by_member(result, currency="USD") == {me.id: D("-30.00"), her.id: D("30.00")}
    assert _by_member(result, currency="EUR") == {me.id: D("20.00"), her.id: D("-20.00")}
    transfers = {
        t.currency: (t.from_member_id, t.to_member_id, t.amount)
        for t in result.transfers_running
    }
    # No netting across currencies.
    assert transfers == {
        "USD": (her.id, me.id, D("30.00")),
        "EUR": (me.id, her.id, D("20.00")),
    }
    _assert_sums_to_zero(result)


@pytest.mark.asyncio
async def test_suggested_transfers_are_small_and_stable(session, test_user, test_workspace):
    group, me, her, _ = await _household(session, test_user, test_workspace.id)
    c = await _member(session, group, test_workspace.id, name="C")
    d = await _member(session, group, test_workspace.id, name="D")
    account = await _make_account(session, test_user.id, test_workspace.id)
    tx = await _make_tx(session, account, "90.00")
    await _share(session, tx, test_user.id, [her, c, d])

    first = await _positions(session, group, test_workspace.id, test_user.id)
    second = await _positions(session, group, test_workspace.id, test_user.id)
    assert first.transfers_running == second.transfers_running
    # Three equal debtors: ties break on member order.
    assert [(t.from_member_id, t.to_member_id, t.amount) for t in first.transfers_running] == [
        (her.id, me.id, D("30.00")),
        (c.id, me.id, D("30.00")),
        (d.id, me.id, D("30.00")),
    ]


# ----------------------------------------------------------------- payer fallback


@pytest.mark.asyncio
async def test_account_owner_who_is_no_member_falls_back_to_the_owner(
    session, test_user, test_workspace
):
    group, me, her, _ = await _household(session, test_user, test_workspace.id)
    outsider = await _make_user(session, "outsider@example.com", test_workspace.id)
    account = await _make_account(session, outsider.id, test_workspace.id)
    tx = await _make_tx(session, account, "100.00")
    await _share(session, tx, test_user.id, [me, her])

    period = await position_service.compute_period(
        session, group.id, test_workspace.id, test_user.id
    )
    assert period is not None
    assert _by_member(period) == {me.id: D("-50.00"), her.id: D("50.00")}
    assert [t.id for t in period.payer_assumed_transactions] == [tx.id]
    assert period.transactions.items[0].payer_member_id == me.id
    assert period.transactions.items[0].payer_assumed is True


@pytest.mark.asyncio
async def test_two_members_linked_to_one_user_fall_back_to_the_owner(
    session, test_user, test_workspace
):
    group, me, her, partner = await _household(session, test_user, test_workspace.id)
    twin = await _member(session, group, test_workspace.id, name="Twin", linked_user_id=partner.id)
    assert twin is not None
    her_account = await _make_account(session, partner.id, test_workspace.id)
    tx = await _make_tx(session, her_account, "100.00")
    await _share(session, tx, test_user.id, [me, her])

    period = await position_service.compute_period(
        session, group.id, test_workspace.id, test_user.id
    )
    assert period is not None
    assert _by_member(period) == {me.id: D("-50.00"), her.id: D("50.00"), twin.id: D("0")}
    assert [t.id for t in period.payer_assumed_transactions] == [tx.id]


@pytest.mark.asyncio
async def test_owner_paid_transaction_is_not_payer_assumed(session, test_user, test_workspace):
    group, me, her, _ = await _household(session, test_user, test_workspace.id)
    account = await _make_account(session, test_user.id, test_workspace.id)
    tx = await _make_tx(session, account, "100.00")
    await _share(session, tx, test_user.id, [me, her])

    period = await position_service.compute_period(
        session, group.id, test_workspace.id, test_user.id
    )
    assert period is not None
    assert period.payer_assumed_transactions == []


@pytest.mark.asyncio
async def test_group_with_no_owner_member_counts_shares_only(session, test_user, test_workspace):
    group = await group_service.create_group(
        session, test_workspace.id, test_user.id, GroupCreate(name="NoOwner")
    )
    a = await _member(session, group, test_workspace.id, name="A")
    b = await _member(session, group, test_workspace.id, name="B")
    account = await _make_account(session, test_user.id, test_workspace.id)
    tx = await _make_tx(session, account, "20.00")
    await _share(session, tx, test_user.id, [a, b])

    period = await position_service.compute_period(
        session, group.id, test_workspace.id, test_user.id
    )
    assert period is not None
    assert period.owner_member_id is None
    # The owner's side is implicit: nobody is credited as payer.
    assert _by_member(period) == {a.id: D("10.00"), b.id: D("10.00")}
    assert period.transactions.items[0].payer_member_id is None
    assert period.payer_assumed_transactions == []
    assert period.transfers_running == []


# ----------------------------------------------------------------- viewers


@pytest.mark.asyncio
async def test_positions_are_identical_for_every_viewer(session, test_user, test_workspace):
    """The owner, a linked member, another workspace member and a
    cross-workspace member all get the same figures, with a clean session
    per read and on two successive reads in one session."""
    ws = test_workspace.id
    group, me, her, partner = await _household(session, test_user, ws)
    bystander = await _make_user(session, "bystander@example.com", ws)
    remote = await _make_user(session, "remote@example.com")
    remote_ws = await workspace_service.create_personal_workspace_for_user(
        session, remote, commit=True
    )
    remote_member = await _member(session, group, ws, name="Remote", email="remote@example.com")
    assert remote_member is not None and remote_member.linked_user_id == remote.id

    account = await _make_account(session, test_user.id, ws)
    her_account = await _make_account(session, partner.id, ws)
    tx = await _make_tx(session, account, "90.00")
    await _share(session, tx, test_user.id, [me, her, remote_member])
    tx2 = await _make_tx(session, her_account, "50.00")
    await _share(session, tx2, test_user.id, [me, her])
    await _contribute(session, group, ws, test_user.id, remote_member, me, "10.00")

    viewers = [
        (test_user.id, ws),
        (partner.id, ws),
        (bystander.id, ws),
        (remote.id, remote_ws.id),
    ]
    group_id, me_id = group.id, me.id

    async def read(user_id, workspace_id):
        # Go through the group service first, as a request does: this is
        # what rewrites `is_self` on the loaded members.
        assert await group_service.get_group_visible(session, group_id, workspace_id, user_id)
        positions = await position_service.compute_positions(
            session, group_id, workspace_id, user_id
        )
        balances = await balance_service.compute_balances(
            session, group_id, workspace_id, user_id
        )
        assert positions is not None and balances is not None
        return positions.model_dump(), balances

    # A clean session per read: members are loaded and tagged afresh.
    clean = []
    for user_id, workspace_id in viewers:
        session.expire_all()
        session.expunge_all()
        clean.append(await read(user_id, workspace_id))
    # Successive reads in one session.
    successive = [await read(user_id, workspace_id) for user_id, workspace_id in viewers]

    expected = clean[0]
    assert expected[0]["owner_member_id"] == me_id
    assert expected[1]["self_member_id"] == me_id
    assert expected[1]["lines"]
    for got in clean[1:] + successive:
        assert got == expected


@pytest.mark.asyncio
async def test_positions_hidden_from_a_stranger(session, test_user, test_workspace):
    group, *_ = await _household(session, test_user, test_workspace.id)
    stranger = await _make_user(session, "stranger@example.com")
    stranger_ws = await workspace_service.create_personal_workspace_for_user(
        session, stranger, commit=True
    )
    assert (
        await position_service.compute_positions(session, group.id, stranger_ws.id, stranger.id)
        is None
    )
    assert (
        await position_service.compute_period(session, group.id, stranger_ws.id, stranger.id)
        is None
    )


# ----------------------------------------------------------------- the period response


@pytest.mark.asyncio
async def test_period_breaks_costs_down_by_category_with_income_apart(
    session, test_user, test_workspace, test_categories
):
    group, me, her, _ = await _household(session, test_user, test_workspace.id)
    account = await _make_account(session, test_user.id, test_workspace.id)
    food, housing = test_categories[0], test_categories[1]
    facts = [
        ("100.00", "debit", food.id, date(2026, 5, 2)),
        ("60.00", "debit", food.id, date(2026, 5, 9)),
        ("30.00", "debit", None, date(2026, 5, 9)),
        ("4000.00", "credit", housing.id, date(2026, 5, 3)),
        ("999.00", "debit", food.id, date(2026, 4, 30)),
    ]
    for amount, type_, category_id, when in facts:
        tx = await _make_tx(session, account, amount, type_=type_, when=when)
        tx.category_id = category_id
        await _share(session, tx, test_user.id, [me, her], "percent", ["60", "40"])
    await _contribute(
        session, group, test_workspace.id, test_user.id, her, me, "500.00", when=date(2026, 5, 25)
    )
    await _contribute(
        session, group, test_workspace.id, test_user.id, her, me, "1.00", when=date(2026, 6, 1)
    )

    period = await position_service.compute_period(
        session,
        group.id,
        test_workspace.id,
        test_user.id,
        start=date(2026, 5, 1),
        end=date(2026, 6, 1),
        page=1,
        page_size=2,
    )
    assert period is not None

    costs = {ln.category_id: ln for ln in period.costs}
    assert costs[food.id].total == D("160.00")
    assert costs[food.id].category_name == food.name
    assert {s.member_id: s.amount for s in costs[food.id].shares} == {
        me.id: D("96.00"),
        her.id: D("64.00"),
    }
    assert costs[None].total == D("30.00")
    # Shared income is its own list, as magnitudes.
    assert [(ln.category_id, ln.total) for ln in period.shared_income] == [
        (housing.id, D("4000.00"))
    ]
    assert {s.member_id: s.amount for s in period.shared_income[0].shares} == {
        me.id: D("2400.00"),
        her.id: D("1600.00"),
    }

    # Only the period's contributions are listed.
    assert [c.amount for c in period.contributions] == [D("500.00")]

    # The list is paged, newest first; the totals are not.
    assert period.transactions.total == 4
    assert period.transactions.page_size == 2
    assert sorted(t.amount for t in period.transactions.items) == [D("30.00"), D("60.00")]
    credit = (
        await position_service.compute_period(
            session,
            group.id,
            test_workspace.id,
            test_user.id,
            start=date(2026, 5, 1),
            end=date(2026, 6, 1),
            page=2,
            page_size=2,
        )
    )
    assert credit is not None
    assert [t.type for t in credit.transactions.items] == ["credit", "debit"]
    assert credit.transactions.items[0].shared_total == D("-4000.00")

    # Her May position: 40 % of (190 - 4000), minus 500 contributed.
    assert _by_member(period)[her.id] == D("-2024.00")
    assert _by_member(period, "backlog")[her.id] == D("399.60")
