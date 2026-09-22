"""My spending means the same everywhere.

Two users in one workspace, as the household has. She pays 1 000 of
groceries from her account and the cost is shared 50/50, so the money
left her account but half of it was his life. What every page should
then say depends on one thing only — whose consumption it is measuring:

    the user filter on him       500, his share of what she paid
    the user filter on her       500, her share of what she paid
    no filter                    1 000, the household's, counted once
    a collection of her accounts 1 000, cash flow, no share adjustment

The subject is explicit, so the same question asked from his login and
from hers gives the same number.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.budget import Budget
from app.models.category import Category
from app.models.transaction import Transaction
from app.models.user import User
from app.models.workspace import WorkspaceMember
from app.schemas.group import GroupCreate, GroupMemberCreate
from app.schemas.transaction_split import (
    TransactionSplitInput,
    TransactionSplitsInput,
)
from app.services import (
    budget_service,
    dashboard_service,
    group_service,
    report_service,
    split_service,
)

GROCERIES = Decimal("1000.00")


def _py_to_char(value, fmt):
    """The income/expenses report buckets periods with PostgreSQL's
    `to_char`. The suite runs on SQLite, so register the same minimal
    emulation the other report tests use."""
    if value is None:
        return None
    y, m, d = str(value)[:10].split("-")
    return {"YYYY-MM-DD": f"{y}-{m}-{d}", "YYYY-MM": f"{y}-{m}", "YYYY": y}.get(
        fmt, str(value)[:10]
    )


@pytest.fixture
async def to_char(session: AsyncSession):
    raw = await session.connection()

    def _do(sync_conn):
        sync_conn.connection.dbapi_connection.create_function("to_char", 2, _py_to_char)

    await raw.run_sync(_do)
    yield


def _usd(user: User) -> None:
    prefs = dict(user.preferences or {})
    prefs["currency_display"] = "USD"
    user.preferences = prefs


async def _household(session: AsyncSession, test_user, test_workspace):
    """His account, her account, and a household group they both belong
    to. Both accounts live in the one shared workspace."""
    import bcrypt

    _usd(test_user)

    partner_user = User(
        id=uuid.uuid4(),
        email=f"partner-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password=bcrypt.hashpw(b"x", bcrypt.gensalt()).decode(),
        is_active=True,
        is_verified=True,
    )
    _usd(partner_user)
    session.add(partner_user)
    await session.flush()
    session.add(
        WorkspaceMember(
            id=uuid.uuid4(),
            workspace_id=test_workspace.id,
            user_id=partner_user.id,
            role="member",
        )
    )

    accounts = {}
    for key, owner in (("mine", test_user.id), ("hers", partner_user.id)):
        account = Account(
            id=uuid.uuid4(),
            user_id=owner,
            workspace_id=test_workspace.id,
            name=key,
            type="checking",
            balance=Decimal("0"),
            currency="USD",
        )
        session.add(account)
        accounts[key] = account
    await session.flush()

    group = await group_service.create_group(
        session,
        test_workspace.id,
        test_user.id,
        GroupCreate(name="Home", kind="household", default_currency="USD"),
    )
    me = await group_service.create_member(
        session, group.id, test_workspace.id, GroupMemberCreate(name="Me", is_self=True)
    )
    partner = await group_service.create_member(
        session,
        group.id,
        test_workspace.id,
        GroupMemberCreate(name="Partner", linked_user_id=partner_user.id),
    )
    await session.commit()
    return {
        "group": group,
        "me": me,
        "partner": partner,
        "partner_user": partner_user,
        **accounts,
    }


async def _tx(
    session: AsyncSession,
    user_id,
    workspace_id,
    account_id,
    amount: Decimal,
    *,
    type_: str = "debit",
    when: date,
    category_id=None,
    description: str = "tx",
) -> Transaction:
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=user_id,
        workspace_id=workspace_id,
        account_id=account_id,
        category_id=category_id,
        description=description,
        amount=amount,
        currency="USD",
        amount_primary=amount,
        date=when,
        effective_date=when,
        type=type_,
        source="sync",
        status="posted",
        created_at=datetime.now(timezone.utc),
    )
    session.add(tx)
    await session.flush()
    return tx


async def _share_evenly(session, home, tx, author_id) -> None:
    await split_service.replace_splits(
        session,
        tx,
        TransactionSplitsInput(
            share_type="equal",
            splits=[
                TransactionSplitInput(group_member_id=home["me"].id),
                TransactionSplitInput(group_member_id=home["partner"].id),
            ],
        ),
        author_id,
    )


async def _groceries_category(session, test_user, test_workspace) -> Category:
    category = Category(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Groceries",
        icon="shopping-cart",
        color="#F59E0B",
    )
    session.add(category)
    await session.flush()
    return category


async def _her_shared_groceries(session, test_user, test_workspace):
    """She pays 1 000 of groceries from her own account, shared 50/50."""
    home = await _household(session, test_user, test_workspace)
    category = await _groceries_category(session, test_user, test_workspace)
    when = date.today().replace(day=15)
    if when > date.today():
        when = date.today()
    tx = await _tx(
        session,
        home["partner_user"].id,
        test_workspace.id,
        home["hers"].id,
        GROCERIES,
        when=when,
        category_id=category.id,
        description="Groceries",
    )
    await _share_evenly(session, home, tx, test_user.id)
    await session.commit()
    return home, category, tx, when


async def _groceries_on_the_dashboard(
    session, test_workspace, viewer_id, category, month, **kwargs
) -> float:
    rows = await dashboard_service.get_spending_by_category(
        session, test_workspace.id, viewer_id, month, **kwargs
    )
    for row in rows:
        if row.category_id == str(category.id):
            return row.total
    return 0.0


async def _groceries_in_the_budget(
    session, test_workspace, viewer_id, category, month, **kwargs
) -> float:
    rows = await budget_service.get_budget_vs_actual(
        session, test_workspace.id, viewer_id, month, **kwargs
    )
    for row in rows:
        if row.category_id == category.id:
            return float(row.actual_amount)
    return 0.0


async def _groceries_in_the_report(
    session, test_workspace, viewer_id, category, **kwargs
) -> float:
    report = await report_service.get_income_expenses_report(
        session, test_workspace.id, viewer_id, months=1, interval="monthly",
        currency="USD", **kwargs
    )
    for item in report.composition:
        if item.key == str(category.id) and item.group == "expenses":
            return item.value
    return 0.0


# ───────────────────── the four ways to look at 1 000 ──────────────────


@pytest.mark.asyncio
async def test_the_user_filter_shows_each_persons_share_of_what_she_paid(
    session: AsyncSession, test_user, test_workspace
):
    home, category, _tx_row, when = await _her_shared_groceries(
        session, test_user, test_workspace
    )
    month = when.replace(day=1)

    on_him = await _groceries_on_the_dashboard(
        session, test_workspace, test_user.id, category, month,
        filter_user_id=test_user.id,
    )
    on_her = await _groceries_on_the_dashboard(
        session, test_workspace, test_user.id, category, month,
        filter_user_id=home["partner_user"].id,
    )
    no_filter = await _groceries_on_the_dashboard(
        session, test_workspace, test_user.id, category, month
    )
    her_collection = await _groceries_on_the_dashboard(
        session, test_workspace, test_user.id, category, month,
        account_ids=[home["hers"].id],
    )

    assert on_him == pytest.approx(500.0, abs=0.01)
    assert on_her == pytest.approx(500.0, abs=0.01)
    assert no_filter == pytest.approx(1000.0, abs=0.01)
    assert her_collection == pytest.approx(1000.0, abs=0.01)


@pytest.mark.asyncio
async def test_the_dashboard_totals_follow_the_same_four_readings(
    session: AsyncSession, test_user, test_workspace
):
    home, _category, _tx_row, when = await _her_shared_groceries(
        session, test_user, test_workspace
    )
    month = when.replace(day=1)

    async def expenses(**kwargs) -> float:
        summary = await dashboard_service.get_summary(
            session, test_workspace.id, test_user.id, month, **kwargs
        )
        return summary.monthly_expenses_primary

    assert await expenses(filter_user_id=test_user.id) == pytest.approx(500.0, abs=0.01)
    assert await expenses(
        filter_user_id=home["partner_user"].id
    ) == pytest.approx(500.0, abs=0.01)
    assert await expenses() == pytest.approx(1000.0, abs=0.01)
    assert await expenses(account_ids=[home["hers"].id]) == pytest.approx(
        1000.0, abs=0.01
    )


@pytest.mark.asyncio
async def test_budgets_take_the_user_filter(
    session: AsyncSession, test_user, test_workspace
):
    home, category, _tx_row, when = await _her_shared_groceries(
        session, test_user, test_workspace
    )
    month = when.replace(day=1)
    session.add(
        Budget(
            id=uuid.uuid4(),
            user_id=test_user.id,
            workspace_id=test_workspace.id,
            category_id=category.id,
            amount=Decimal("2000.00"),
            currency="USD",
            month=month,
            is_recurring=True,
        )
    )
    await session.commit()

    on_him = await _groceries_in_the_budget(
        session, test_workspace, test_user.id, category, month,
        filter_user_id=test_user.id,
    )
    on_her = await _groceries_in_the_budget(
        session, test_workspace, test_user.id, category, month,
        filter_user_id=home["partner_user"].id,
    )
    no_filter = await _groceries_in_the_budget(
        session, test_workspace, test_user.id, category, month
    )

    assert on_him == pytest.approx(500.0, abs=0.01)
    assert on_her == pytest.approx(500.0, abs=0.01)
    assert no_filter == pytest.approx(1000.0, abs=0.01)


@pytest.mark.asyncio
async def test_the_report_composition_takes_the_user_filter(
    session: AsyncSession, test_user, test_workspace, to_char
):
    home, category, _tx_row, _when = await _her_shared_groceries(
        session, test_user, test_workspace
    )

    on_him = await _groceries_in_the_report(
        session, test_workspace, test_user.id, category,
        filter_user_id=test_user.id,
    )
    on_her = await _groceries_in_the_report(
        session, test_workspace, test_user.id, category,
        filter_user_id=home["partner_user"].id,
    )
    no_filter = await _groceries_in_the_report(
        session, test_workspace, test_user.id, category
    )
    her_collection = await _groceries_in_the_report(
        session, test_workspace, test_user.id, category,
        account_ids=[home["hers"].id],
    )

    assert on_him == pytest.approx(500.0, abs=0.01)
    assert on_her == pytest.approx(500.0, abs=0.01)
    assert no_filter == pytest.approx(1000.0, abs=0.01)
    assert her_collection == pytest.approx(1000.0, abs=0.01)


# ─────────────────── the subject, not the logged-in user ────────────────


@pytest.mark.asyncio
async def test_her_picture_reads_the_same_from_his_login_and_from_hers(
    session: AsyncSession, test_user, test_workspace
):
    """The share helpers take the consumption subject explicitly, so who
    is holding the session decides nothing about whose shares count."""
    home, category, _tx_row, when = await _her_shared_groceries(
        session, test_user, test_workspace
    )
    month = when.replace(day=1)

    from_his_login = await _groceries_on_the_dashboard(
        session, test_workspace, test_user.id, category, month,
        filter_user_id=home["partner_user"].id,
    )
    from_her_login = await _groceries_on_the_dashboard(
        session, test_workspace, home["partner_user"].id, category, month,
        filter_user_id=home["partner_user"].id,
    )
    assert from_his_login == pytest.approx(from_her_login, abs=0.01)
    assert from_his_login == pytest.approx(500.0, abs=0.01)


@pytest.mark.asyncio
async def test_with_no_filter_the_workspace_total_is_the_same_for_both_of_them(
    session: AsyncSession, test_user, test_workspace
):
    """Shares between two members of one workspace cancel: the household
    total is 1 000 whoever asks, not 1 500 for the one who is not the
    payer."""
    home, category, _tx_row, when = await _her_shared_groceries(
        session, test_user, test_workspace
    )
    month = when.replace(day=1)

    his = await _groceries_on_the_dashboard(
        session, test_workspace, test_user.id, category, month
    )
    hers = await _groceries_on_the_dashboard(
        session, test_workspace, home["partner_user"].id, category, month
    )
    assert his == pytest.approx(1000.0, abs=0.01)
    assert hers == pytest.approx(1000.0, abs=0.01)


# ────────────────────── a refund cancels its purchase ───────────────────


@pytest.mark.asyncio
async def test_a_shared_refund_leaves_the_category_at_zero_everywhere(
    session: AsyncSession, test_user, test_workspace, to_char
):
    home, category, _tx_row, when = await _her_shared_groceries(
        session, test_user, test_workspace
    )
    refund = await _tx(
        session,
        home["partner_user"].id,
        test_workspace.id,
        home["hers"].id,
        GROCERIES,
        type_="credit",
        when=when,
        category_id=category.id,
        description="Groceries refunded",
    )
    await _share_evenly(session, home, refund, test_user.id)
    month = when.replace(day=1)
    session.add(
        Budget(
            id=uuid.uuid4(),
            user_id=test_user.id,
            workspace_id=test_workspace.id,
            category_id=category.id,
            amount=Decimal("2000.00"),
            currency="USD",
            month=month,
            is_recurring=True,
        )
    )
    await session.commit()

    for filters in (
        {},
        {"filter_user_id": test_user.id},
        {"filter_user_id": home["partner_user"].id},
    ):
        assert await _groceries_on_the_dashboard(
            session, test_workspace, test_user.id, category, month, **filters
        ) == pytest.approx(0.0, abs=0.01)
        assert await _groceries_in_the_budget(
            session, test_workspace, test_user.id, category, month, **filters
        ) == pytest.approx(0.0, abs=0.01)
        assert await _groceries_in_the_report(
            session, test_workspace, test_user.id, category, **filters
        ) == pytest.approx(0.0, abs=0.01)


@pytest.mark.asyncio
async def test_a_collection_still_shows_cash_flow_with_no_share_adjustment(
    session: AsyncSession, test_user, test_workspace
):
    """A refund on her account is a credit, and a collection is a cash
    view: the debit still reads 1 000 there."""
    home, category, _tx_row, when = await _her_shared_groceries(
        session, test_user, test_workspace
    )
    refund = await _tx(
        session,
        home["partner_user"].id,
        test_workspace.id,
        home["hers"].id,
        GROCERIES,
        type_="credit",
        when=when,
        category_id=category.id,
        description="Groceries refunded",
    )
    await _share_evenly(session, home, refund, test_user.id)
    await session.commit()

    assert await _groceries_on_the_dashboard(
        session, test_workspace, test_user.id, category, when.replace(day=1),
        account_ids=[home["hers"].id],
    ) == pytest.approx(1000.0, abs=0.01)


# ───────────────────── nothing moves for one user alone ─────────────────


@pytest.mark.asyncio
async def test_a_single_user_workspace_with_an_outside_member_is_unchanged(
    session: AsyncSession, test_user, test_workspace, to_char
):
    """The figures upstream gives: he fronts 120 for himself and three
    people who have no user here, so his consumption is his 30 share —
    in the dashboard, in a budget and in the report's composition, with
    no filter and with the filter on himself."""
    _usd(test_user)
    when = date.today().replace(day=10)
    if when > date.today():
        when = date.today()
    category = await _groceries_category(session, test_user, test_workspace)
    account = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="mine",
        type="checking",
        balance=Decimal("0"),
        currency="USD",
    )
    session.add(account)
    await session.flush()
    tx = await _tx(
        session,
        test_user.id,
        test_workspace.id,
        account.id,
        Decimal("120.00"),
        when=when,
        category_id=category.id,
        description="Dinner",
    )
    group = await group_service.create_group(
        session,
        test_workspace.id,
        test_user.id,
        GroupCreate(name="Friends", default_currency="USD"),
    )
    members = []
    for i, name in enumerate(("Me", "A", "B", "C")):
        member = await group_service.create_member(
            session, group.id, test_workspace.id,
            GroupMemberCreate(name=name, is_self=(i == 0)),
        )
        assert member is not None
        members.append(member)
    await split_service.replace_splits(
        session,
        tx,
        TransactionSplitsInput(
            share_type="equal",
            splits=[TransactionSplitInput(group_member_id=m.id) for m in members],
        ),
        test_user.id,
    )
    month = when.replace(day=1)
    session.add(
        Budget(
            id=uuid.uuid4(),
            user_id=test_user.id,
            workspace_id=test_workspace.id,
            category_id=category.id,
            amount=Decimal("500.00"),
            currency="USD",
            month=month,
            is_recurring=True,
        )
    )
    await session.commit()

    for filters in ({}, {"filter_user_id": test_user.id}):
        assert await _groceries_on_the_dashboard(
            session, test_workspace, test_user.id, category, month, **filters
        ) == pytest.approx(30.0, abs=0.01)
        assert await _groceries_in_the_budget(
            session, test_workspace, test_user.id, category, month, **filters
        ) == pytest.approx(30.0, abs=0.01)
        assert await _groceries_in_the_report(
            session, test_workspace, test_user.id, category, **filters
        ) == pytest.approx(30.0, abs=0.01)

    summary = await dashboard_service.get_summary(
        session, test_workspace.id, test_user.id, month
    )
    assert summary.monthly_expenses_primary == pytest.approx(30.0, abs=0.01)


@pytest.mark.asyncio
async def test_a_transaction_taken_out_of_the_pot_is_still_its_payers_cost(
    session: AsyncSession, test_user, test_workspace
):
    """Shared 100 % on its payer, a personal purchase moves no position —
    but it is still the whole of that person's consumption."""
    home = await _household(session, test_user, test_workspace)
    category = await _groceries_category(session, test_user, test_workspace)
    when = date.today().replace(day=12)
    if when > date.today():
        when = date.today()
    tx = await _tx(
        session,
        home["partner_user"].id,
        test_workspace.id,
        home["hers"].id,
        Decimal("300.00"),
        when=when,
        category_id=category.id,
        description="Her own shoes",
    )
    await split_service.replace_splits(
        session,
        tx,
        TransactionSplitsInput(
            share_type="percent",
            splits=[
                TransactionSplitInput(
                    group_member_id=home["partner"].id, share_pct=Decimal("100")
                )
            ],
        ),
        test_user.id,
    )
    await session.commit()
    month = when.replace(day=1)

    assert await _groceries_on_the_dashboard(
        session, test_workspace, test_user.id, category, month,
        filter_user_id=home["partner_user"].id,
    ) == pytest.approx(300.0, abs=0.01)
    assert await _groceries_on_the_dashboard(
        session, test_workspace, test_user.id, category, month,
        filter_user_id=test_user.id,
    ) == pytest.approx(0.0, abs=0.01)


@pytest.mark.asyncio
async def test_shared_income_lowers_what_the_other_carries(
    session: AsyncSession, test_user, test_workspace
):
    """Rent of 5 000 lands on his account and is shared 60/40. Her
    picture gains 2 000 of income; his keeps 3 000."""
    home = await _household(session, test_user, test_workspace)
    when = date.today().replace(day=8)
    if when > date.today():
        when = date.today()
    rent = await _tx(
        session,
        test_user.id,
        test_workspace.id,
        home["mine"].id,
        Decimal("5000.00"),
        type_="credit",
        when=when,
        description="Rent",
    )
    await split_service.replace_splits(
        session,
        rent,
        TransactionSplitsInput(
            share_type="percent",
            splits=[
                TransactionSplitInput(
                    group_member_id=home["me"].id, share_pct=Decimal("60")
                ),
                TransactionSplitInput(
                    group_member_id=home["partner"].id, share_pct=Decimal("40")
                ),
            ],
        ),
        test_user.id,
    )
    await session.commit()
    month = when.replace(day=1)

    async def income(**kwargs) -> float:
        summary = await dashboard_service.get_summary(
            session, test_workspace.id, test_user.id, month, **kwargs
        )
        return summary.monthly_income_primary

    assert await income(filter_user_id=test_user.id) == pytest.approx(3000.0, abs=0.01)
    assert await income(
        filter_user_id=home["partner_user"].id
    ) == pytest.approx(2000.0, abs=0.01)
    assert await income() == pytest.approx(5000.0, abs=0.01)


# ──────────────────────────── over the wire ─────────────────────────────


@pytest.mark.asyncio
async def test_every_page_the_collection_filter_reaches_takes_the_user_filter(
    session: AsyncSession, client, auth_headers, test_user, test_workspace
):
    home, category, _tx_row, when = await _her_shared_groceries(
        session, test_user, test_workspace
    )
    her = str(home["partner_user"].id)
    month = when.replace(day=1).isoformat()

    for path, params in (
        ("/api/dashboard/summary", {"month": month}),
        ("/api/dashboard/spending-by-category", {"month": month}),
        ("/api/dashboard/monthly-trend", {"months": 1}),
        ("/api/dashboard/balance-history", {"month": month}),
        ("/api/reports/net-worth", {"months": 1}),
        ("/api/reports/cash-flow", {"months": 1}),
        ("/api/budgets/comparison", {"month": month}),
    ):
        response = await client.get(
            path, params={**params, "user_id": her}, headers=auth_headers
        )
        assert response.status_code == 200, f"{path}: {response.text}"

    spending = await client.get(
        "/api/dashboard/spending-by-category",
        params={"month": month, "user_id": her},
        headers=auth_headers,
    )
    rows = {row["category_id"]: row["total"] for row in spending.json()}
    assert rows[str(category.id)] == pytest.approx(500.0, abs=0.01)


@pytest.mark.asyncio
async def test_the_user_filter_refuses_somebody_outside_the_workspace(
    session: AsyncSession, client, auth_headers, test_user, test_workspace
):
    import bcrypt

    stranger = User(
        id=uuid.uuid4(),
        email=f"stranger-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password=bcrypt.hashpw(b"x", bcrypt.gensalt()).decode(),
        is_active=True,
        is_verified=True,
    )
    session.add(stranger)
    await session.commit()

    response = await client.get(
        "/api/dashboard/spending-by-category",
        params={"user_id": str(stranger.id)},
        headers=auth_headers,
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_a_period_with_no_activity_for_her_is_zero_not_the_households(
    session: AsyncSession, test_user, test_workspace
):
    """The filter narrows the scope as well as the subject: a month she
    spent nothing in reads as nothing, even though he spent."""
    home = await _household(session, test_user, test_workspace)
    category = await _groceries_category(session, test_user, test_workspace)
    when = date.today().replace(day=5)
    if when > date.today():
        when = date.today()
    await _tx(
        session,
        test_user.id,
        test_workspace.id,
        home["mine"].id,
        Decimal("400.00"),
        when=when,
        category_id=category.id,
        description="His own thing",
    )
    await session.commit()
    month = when.replace(day=1)

    assert await _groceries_on_the_dashboard(
        session, test_workspace, test_user.id, category, month,
        filter_user_id=home["partner_user"].id,
    ) == pytest.approx(0.0, abs=0.01)
    assert await _groceries_on_the_dashboard(
        session, test_workspace, test_user.id, category, month,
        filter_user_id=test_user.id,
    ) == pytest.approx(400.0, abs=0.01)


@pytest.mark.asyncio
async def test_the_income_and_expenses_series_follows_the_subject(
    session: AsyncSession, test_user, test_workspace, to_char
):
    """Not just the composition: the report's own totals."""
    home, _category, _tx_row, _when = await _her_shared_groceries(
        session, test_user, test_workspace
    )

    async def expenses(**kwargs) -> float:
        report = await report_service.get_income_expenses_report(
            session, test_workspace.id, test_user.id, months=1, interval="monthly",
            currency="USD", **kwargs
        )
        return next(b.value for b in report.summary.breakdowns if b.key == "expenses")

    assert await expenses(filter_user_id=test_user.id) == pytest.approx(500.0, abs=0.01)
    assert await expenses(
        filter_user_id=home["partner_user"].id
    ) == pytest.approx(500.0, abs=0.01)
    assert await expenses() == pytest.approx(1000.0, abs=0.01)
    assert await expenses(account_ids=[home["hers"].id]) == pytest.approx(
        1000.0, abs=0.01
    )


@pytest.mark.asyncio
async def test_a_months_trend_follows_the_subject(
    session: AsyncSession, test_user, test_workspace
):
    home, _category, _tx_row, when = await _her_shared_groceries(
        session, test_user, test_workspace
    )
    key = f"{when.year:04d}-{when.month:02d}"

    async def expenses(**kwargs) -> float:
        trend = await dashboard_service.get_monthly_trend(
            session, test_workspace.id, test_user.id, months=1, **kwargs
        )
        return next((row.expenses for row in trend if row.month == key), 0.0)

    assert await expenses(filter_user_id=test_user.id) == pytest.approx(500.0, abs=0.01)
    assert await expenses(
        filter_user_id=home["partner_user"].id
    ) == pytest.approx(500.0, abs=0.01)
    assert await expenses() == pytest.approx(1000.0, abs=0.01)


@pytest.mark.asyncio
async def test_the_user_filter_narrows_the_balance_to_her_own_accounts(
    session: AsyncSession, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    when = date.today() - timedelta(days=1)
    await _tx(
        session,
        home["partner_user"].id,
        test_workspace.id,
        home["hers"].id,
        Decimal("700.00"),
        type_="credit",
        when=when,
        description="Salary",
    )
    await _tx(
        session,
        test_user.id,
        test_workspace.id,
        home["mine"].id,
        Decimal("100.00"),
        type_="credit",
        when=when,
        description="His salary",
    )
    await session.commit()
    month = date.today().replace(day=1)

    hers = await dashboard_service.get_summary(
        session, test_workspace.id, test_user.id, month,
        filter_user_id=home["partner_user"].id,
    )
    both = await dashboard_service.get_summary(
        session, test_workspace.id, test_user.id, month
    )
    assert hers.total_balance_primary == pytest.approx(700.0, abs=0.01)
    assert both.total_balance_primary == pytest.approx(800.0, abs=0.01)


# ───────────────── a workspace is a separate set of books ───────────────


async def _second_workspace(session: AsyncSession, user):
    """Another workspace of his — a personal one beside the household,
    which is what everybody has after registering."""
    from app.models.workspace import Workspace

    workspace = Workspace(
        id=uuid.uuid4(),
        name="Personal",
        kind="personal",
        created_by_user_id=user.id,
        default_currency="USD",
        locale="en",
    )
    session.add(workspace)
    await session.flush()
    session.add(
        WorkspaceMember(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            user_id=user.id,
            role="owner",
        )
    )
    account = Account(
        id=uuid.uuid4(),
        user_id=user.id,
        workspace_id=workspace.id,
        name="Personal checking",
        type="checking",
        balance=Decimal("0"),
        currency="USD",
    )
    session.add(account)
    await session.flush()
    return workspace, account


@pytest.mark.asyncio
async def test_the_household_never_reaches_his_other_workspace(
    session: AsyncSession, test_user, test_workspace, to_char
):
    """A workspace is its own set of books. His share of the household's
    groceries belongs to the household's figures and to no other."""
    home, category, _tx_row, when = await _her_shared_groceries(
        session, test_user, test_workspace
    )
    # The same again, paid by him, plus a shared refund — the three
    # shapes that could cross the line.
    his_groceries = await _tx(
        session,
        test_user.id,
        test_workspace.id,
        home["mine"].id,
        GROCERIES,
        when=when,
        category_id=category.id,
        description="His groceries",
    )
    await _share_evenly(session, home, his_groceries, test_user.id)
    refund = await _tx(
        session,
        test_user.id,
        test_workspace.id,
        home["mine"].id,
        Decimal("200.00"),
        type_="credit",
        when=when,
        category_id=category.id,
        description="Refund",
    )
    await _share_evenly(session, home, refund, test_user.id)

    personal, personal_account = await _second_workspace(session, test_user)
    personal_category = Category(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=personal.id,
        name="Books",
        icon="book",
        color="#3B82F6",
    )
    session.add(personal_category)
    await session.flush()
    await _tx(
        session,
        test_user.id,
        personal.id,
        personal_account.id,
        Decimal("200.00"),
        when=when,
        category_id=personal_category.id,
        description="A book",
    )
    await session.commit()
    month = when.replace(day=1)

    # His personal workspace shows its own 200 and nothing of the house.
    summary = await dashboard_service.get_summary(
        session, personal.id, test_user.id, month
    )
    assert summary.monthly_expenses_primary == pytest.approx(200.0, abs=0.01)
    assert summary.monthly_income_primary == pytest.approx(0.0, abs=0.01)

    rows = await dashboard_service.get_spending_by_category(
        session, personal.id, test_user.id, month
    )
    assert {row.category_name for row in rows} == {"Books"}
    assert rows[0].total == pytest.approx(200.0, abs=0.01)

    trend = await dashboard_service.get_monthly_trend(
        session, personal.id, test_user.id, months=1
    )
    key = f"{when.year:04d}-{when.month:02d}"
    assert [row.expenses for row in trend if row.month == key] == [
        pytest.approx(200.0, abs=0.01)
    ]

    report = await report_service.get_income_expenses_report(
        session, personal.id, test_user.id, months=1, interval="monthly",
        currency="USD",
    )
    totals = {b.key: b.value for b in report.summary.breakdowns}
    assert totals["expenses"] == pytest.approx(200.0, abs=0.01)
    assert {item.key for item in report.composition} == {str(personal_category.id)}

    session.add(
        Budget(
            id=uuid.uuid4(),
            user_id=test_user.id,
            workspace_id=personal.id,
            category_id=personal_category.id,
            amount=Decimal("500.00"),
            currency="USD",
            month=month,
            is_recurring=True,
        )
    )
    await session.commit()
    budget_rows = await budget_service.get_budget_vs_actual(
        session, personal.id, test_user.id, month
    )
    assert {r.category_id for r in budget_rows} == {personal_category.id}
    assert float(budget_rows[0].actual_amount) == pytest.approx(200.0, abs=0.01)

    # And the household's own figures are untouched: 1 000 hers, 1 000 his,
    # 200 back.
    household = await dashboard_service.get_summary(
        session, test_workspace.id, test_user.id, month
    )
    assert household.monthly_expenses_primary == pytest.approx(1800.0, abs=0.01)
    assert await _groceries_on_the_dashboard(
        session, test_workspace, test_user.id, category, month,
        filter_user_id=test_user.id,
    ) == pytest.approx(900.0, abs=0.01)


@pytest.mark.asyncio
async def test_his_other_workspace_does_not_leak_into_the_household_either(
    session: AsyncSession, test_user, test_workspace
):
    home, category, _tx_row, when = await _her_shared_groceries(
        session, test_user, test_workspace
    )
    personal, personal_account = await _second_workspace(session, test_user)
    personal_group = await group_service.create_group(
        session, personal.id, test_user.id,
        GroupCreate(name="Personal pot", default_currency="USD"),
    )
    mine = await group_service.create_member(
        session, personal_group.id, personal.id,
        GroupMemberCreate(name="Me", is_self=True),
    )
    assert mine is not None
    personal_tx = await _tx(
        session,
        test_user.id,
        personal.id,
        personal_account.id,
        Decimal("400.00"),
        when=when,
        category_id=category.id,
        description="Personal, shared with himself",
    )
    await split_service.replace_splits(
        session,
        personal_tx,
        TransactionSplitsInput(
            share_type="percent",
            splits=[
                TransactionSplitInput(
                    group_member_id=mine.id, share_pct=Decimal("100")
                )
            ],
        ),
        test_user.id,
    )
    await session.commit()
    month = when.replace(day=1)

    for filters in ({}, {"filter_user_id": test_user.id}):
        summary = await dashboard_service.get_summary(
            session, test_workspace.id, test_user.id, month, **filters
        )
        expected = 1000.0 if not filters else 500.0
        assert summary.monthly_expenses_primary == pytest.approx(expected, abs=0.01)


# ─────────────── the breakdown adds up to the card above it ─────────────


async def _pie_total(session, test_workspace, viewer_id, month, **kwargs) -> float:
    rows = await dashboard_service.get_spending_by_category(
        session, test_workspace.id, viewer_id, month, **kwargs
    )
    return sum(row.total for row in rows)


async def _composition_totals(session, test_workspace, viewer_id, **kwargs) -> dict:
    report = await report_service.get_income_expenses_report(
        session, test_workspace.id, viewer_id, months=1, interval="monthly",
        currency="USD", **kwargs
    )
    out = {"income": 0.0, "expenses": 0.0}
    for item in report.composition:
        if item.group in out:
            out[item.group] += item.value
    series = {b.key: b.value for b in report.summary.breakdowns}
    return {"composition": out, "series": series}


async def _refunded_groceries(session, test_user, test_workspace):
    """1 000 of groceries and 400 back, both shared 50/50, both on her
    account and in the same month and category."""
    home, category, _tx_row, when = await _her_shared_groceries(
        session, test_user, test_workspace
    )
    refund = await _tx(
        session,
        home["partner_user"].id,
        test_workspace.id,
        home["hers"].id,
        Decimal("400.00"),
        type_="credit",
        when=when,
        category_id=category.id,
        description="Partly refunded",
    )
    await _share_evenly(session, home, refund, test_user.id)
    await session.commit()
    return home, category, when


@pytest.mark.asyncio
async def test_the_dashboard_card_equals_the_sum_of_its_breakdown(
    session: AsyncSession, test_user, test_workspace
):
    home, _category, when = await _refunded_groceries(
        session, test_user, test_workspace
    )
    month = when.replace(day=1)

    for filters, expected in (
        ({}, 600.0),
        ({"filter_user_id": test_user.id}, 300.0),
        ({"filter_user_id": home["partner_user"].id}, 300.0),
    ):
        summary = await dashboard_service.get_summary(
            session, test_workspace.id, test_user.id, month, **filters
        )
        pie = await _pie_total(
            session, test_workspace, test_user.id, month, **filters
        )
        assert summary.monthly_expenses_primary == pytest.approx(expected, abs=0.01)
        assert pie == pytest.approx(expected, abs=0.01)
        # The refund lowered the cost instead of becoming somebody's income.
        assert summary.monthly_income_primary == pytest.approx(0.0, abs=0.01)


@pytest.mark.asyncio
async def test_the_report_series_equals_the_sum_of_its_composition(
    session: AsyncSession, test_user, test_workspace, to_char
):
    home, _category, _when = await _refunded_groceries(
        session, test_user, test_workspace
    )

    for filters, expected in (
        ({}, 600.0),
        ({"filter_user_id": test_user.id}, 300.0),
        ({"filter_user_id": home["partner_user"].id}, 300.0),
    ):
        totals = await _composition_totals(
            session, test_workspace, test_user.id, **filters
        )
        assert totals["series"]["expenses"] == pytest.approx(expected, abs=0.01)
        assert totals["composition"]["expenses"] == pytest.approx(expected, abs=0.01)
        assert totals["series"]["income"] == pytest.approx(0.0, abs=0.01)
        assert totals["composition"]["income"] == pytest.approx(0.0, abs=0.01)


@pytest.mark.asyncio
async def test_an_unshared_refund_is_left_exactly_as_upstream_has_it(
    session: AsyncSession, test_user, test_workspace, to_char
):
    """The credit term is about *shares*. A refund nobody shared still
    counts as income and still leaves its category at full cost, which
    is what upstream does and what this batch must not change."""
    _usd(test_user)
    when = date.today().replace(day=11)
    if when > date.today():
        when = date.today()
    category = await _groceries_category(session, test_user, test_workspace)
    account = Account(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="mine",
        type="checking",
        balance=Decimal("0"),
        currency="USD",
    )
    session.add(account)
    await session.flush()
    await _tx(
        session, test_user.id, test_workspace.id, account.id, GROCERIES,
        when=when, category_id=category.id, description="Groceries",
    )
    await _tx(
        session, test_user.id, test_workspace.id, account.id, Decimal("400.00"),
        type_="credit", when=when, category_id=category.id, description="Refund",
    )
    await session.commit()
    month = when.replace(day=1)

    summary = await dashboard_service.get_summary(
        session, test_workspace.id, test_user.id, month
    )
    assert summary.monthly_expenses_primary == pytest.approx(1000.0, abs=0.01)
    assert summary.monthly_income_primary == pytest.approx(400.0, abs=0.01)
    assert await _groceries_on_the_dashboard(
        session, test_workspace, test_user.id, category, month
    ) == pytest.approx(1000.0, abs=0.01)

    totals = await _composition_totals(session, test_workspace, test_user.id)
    assert totals["series"]["income"] == pytest.approx(400.0, abs=0.01)
    assert totals["composition"]["income"] == pytest.approx(400.0, abs=0.01)


@pytest.mark.asyncio
async def test_shared_income_in_a_category_with_no_costs_stays_income(
    session: AsyncSession, test_user, test_workspace, to_char
):
    """Rent is not a refund: with nothing spent in its category, a shared
    credit is shared income and the credit term leaves it alone."""
    home = await _household(session, test_user, test_workspace)
    when = date.today().replace(day=9)
    if when > date.today():
        when = date.today()
    rent_category = Category(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Rent received",
        icon="home",
        color="#22C55E",
    )
    session.add(rent_category)
    await session.flush()
    rent = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id,
        Decimal("5000.00"), type_="credit", when=when,
        category_id=rent_category.id, description="Rent",
    )
    await _share_evenly(session, home, rent, test_user.id)
    await session.commit()
    month = when.replace(day=1)

    on_him = await dashboard_service.get_summary(
        session, test_workspace.id, test_user.id, month,
        filter_user_id=test_user.id,
    )
    assert on_him.monthly_income_primary == pytest.approx(2500.0, abs=0.01)
    assert on_him.monthly_expenses_primary == pytest.approx(0.0, abs=0.01)

    totals = await _composition_totals(
        session, test_workspace, test_user.id, filter_user_id=test_user.id
    )
    assert totals["series"]["income"] == pytest.approx(2500.0, abs=0.01)
    assert totals["composition"]["income"] == pytest.approx(2500.0, abs=0.01)


# ───────────────── her sparklines, and her closed accounts ──────────────


@pytest.mark.asyncio
async def test_the_category_trends_follow_a_cost_she_never_paid(
    session: AsyncSession, test_user, test_workspace, to_char
):
    """He pays, she carries half. Her report showed a total and a
    breakdown but no sparkline at all, because the trend was only ever
    opened by a row on her own accounts."""
    home = await _household(session, test_user, test_workspace)
    category = await _groceries_category(session, test_user, test_workspace)
    when = date.today().replace(day=14)
    if when > date.today():
        when = date.today()
    tx = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, GROCERIES,
        when=when, category_id=category.id, description="Groceries",
    )
    await _share_evenly(session, home, tx, test_user.id)
    await session.commit()

    report = await report_service.get_income_expenses_report(
        session, test_workspace.id, test_user.id, months=1, interval="monthly",
        currency="USD", filter_user_id=home["partner_user"].id,
    )
    trends = {item.key: item for item in report.category_trend}
    assert str(category.id) in trends, "her groceries have no sparkline"
    assert trends[str(category.id)].total == pytest.approx(500.0, abs=0.01)
    assert trends[str(category.id)].label == "Groceries"
    assert sum(point.value for point in trends[str(category.id)].series) == (
        pytest.approx(500.0, abs=0.01)
    )


@pytest.mark.asyncio
async def test_a_closed_account_is_outside_every_figure(
    session: AsyncSession, test_user, test_workspace
):
    """Every base total drops closed accounts, so a share of a cost on
    one must not come off a figure that never carried it — which read as
    minus 500 of expenses for her."""
    home = await _household(session, test_user, test_workspace)
    category = await _groceries_category(session, test_user, test_workspace)
    when = date.today().replace(day=7)
    if when > date.today():
        when = date.today()
    closed = Account(
        id=uuid.uuid4(),
        user_id=home["partner_user"].id,
        workspace_id=test_workspace.id,
        name="Her old card",
        type="checking",
        balance=Decimal("0"),
        currency="USD",
        is_closed=True,
    )
    session.add(closed)
    await session.flush()
    tx = await _tx(
        session, home["partner_user"].id, test_workspace.id, closed.id,
        GROCERIES, when=when, category_id=category.id, description="On the old card",
    )
    await _share_evenly(session, home, tx, test_user.id)
    await session.commit()
    month = when.replace(day=1)

    for filters in (
        {},
        {"filter_user_id": test_user.id},
        {"filter_user_id": home["partner_user"].id},
    ):
        summary = await dashboard_service.get_summary(
            session, test_workspace.id, test_user.id, month, **filters
        )
        assert summary.monthly_expenses_primary == pytest.approx(0.0, abs=0.01)
        assert summary.monthly_income_primary == pytest.approx(0.0, abs=0.01)
        assert await _groceries_on_the_dashboard(
            session, test_workspace, test_user.id, category, month, **filters
        ) == pytest.approx(0.0, abs=0.01)


@pytest.mark.asyncio
async def test_the_filter_resolves_to_her_open_accounts_only(
    session: AsyncSession, test_user, test_workspace
):
    from app.services._query_filters import user_owned_account_ids

    home = await _household(session, test_user, test_workspace)
    closed = Account(
        id=uuid.uuid4(),
        user_id=home["partner_user"].id,
        workspace_id=test_workspace.id,
        name="Her old card",
        type="checking",
        balance=Decimal("0"),
        currency="USD",
        is_closed=True,
    )
    session.add(closed)
    await session.commit()

    owned = await user_owned_account_ids(
        session, test_workspace.id, home["partner_user"].id
    )
    assert owned == [home["hers"].id]
