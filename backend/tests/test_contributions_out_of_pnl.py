"""A contribution is neither income nor spending, for either member.

Two users in one workspace, as the household has. She transfers 2 000 to
him every month; his account shows the credit, hers the debit. Marked as
a contribution, neither leg may reach a profit-and-loss figure anywhere
in the app — that inflated income is the reason the spec exists — while
both still move their account's balance and stay in the transaction list.

One test per reader #1's checklist names: the dashboard, reports and
their cash-flow baseline, budgets, the transaction list's footer totals,
account statistics, and the forecast.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.budget import Budget
from app.models.category import Category
from app.models.group_settlement import GroupSettlement
from app.models.transaction import Transaction
from app.models.user import User
from app.models.workspace import WorkspaceMember
from app.schemas.transaction_calendar import TransactionCalendarDay
from app.schemas.group import GroupCreate, GroupMemberCreate
from app.services import (
    account_service,
    budget_service,
    dashboard_service,
    group_service,
    report_service,
    transaction_service,
)

TRANSFER = Decimal("2000.00")
GROCERIES = Decimal("300.00")


def _py_to_char(value, fmt):
    """The income/expenses report buckets periods with PostgreSQL's
    `to_char`. The suite runs on SQLite, so register the same minimal
    emulation `test_report_service_coverage` uses."""
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


# ──────────────────────────── the household ─────────────────────────────


async def _household(session: AsyncSession, test_user, test_workspace):
    """His account with her transfer on it, her account with the same
    transfer leaving it, and a household group they both belong to."""
    import bcrypt

    prefs = dict(test_user.preferences or {})
    prefs["currency_display"] = "USD"
    test_user.preferences = prefs

    partner_user = User(
        id=uuid.uuid4(),
        email=f"partner-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password=bcrypt.hashpw(b"x", bcrypt.gensalt()).decode(),
        is_active=True,
        is_verified=True,
    )
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
    type_: str,
    when: date,
    description: str,
    category_id=None,
    status: str = "posted",
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
        status=status,
        created_at=datetime.now(timezone.utc),
    )
    session.add(tx)
    await session.flush()
    return tx


async def _transfer_legs(
    session: AsyncSession, home, test_user, test_workspace, when: date, **kwargs
) -> tuple[Transaction, Transaction]:
    credit = await _tx(
        session,
        test_user.id,
        test_workspace.id,
        home["mine"].id,
        TRANSFER,
        type_="credit",
        when=when,
        description="Monthly transfer",
        **kwargs,
    )
    debit = await _tx(
        session,
        home["partner_user"].id,
        test_workspace.id,
        home["hers"].id,
        TRANSFER,
        type_="debit",
        when=when,
        description="Monthly transfer",
        **kwargs,
    )
    return credit, debit


async def _mark(session: AsyncSession, home, test_workspace, credit, debit) -> None:
    """Both legs, one contribution — the pairing case, written directly
    so the readers below are tested against the shape the guard allows."""
    session.add(
        GroupSettlement(
            id=uuid.uuid4(),
            group_id=home["group"].id,
            workspace_id=test_workspace.id,
            from_member_id=home["partner"].id,
            to_member_id=home["me"].id,
            amount=TRANSFER,
            currency="USD",
            date=credit.date,
            transaction_id=debit.id,
            receiver_transaction_id=credit.id,
        )
    )
    await session.commit()


# ───────────────────────────── the readers ──────────────────────────────


@pytest.mark.asyncio
async def test_the_dashboard_stops_counting_the_transfer_as_income(
    session: AsyncSession, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    today = date.today().replace(day=15)
    credit, debit = await _transfer_legs(session, home, test_user, test_workspace, today)
    await session.commit()

    month = today.replace(day=1)
    before = await dashboard_service.get_summary(
        session, test_workspace.id, test_user.id, month
    )
    assert before.monthly_income_primary == pytest.approx(2000.0, abs=0.01)

    await _mark(session, home, test_workspace, credit, debit)

    after = await dashboard_service.get_summary(
        session, test_workspace.id, test_user.id, month
    )
    assert after.monthly_income_primary == 0.0
    assert after.monthly_expenses_primary == 0.0


@pytest.mark.asyncio
async def test_the_income_and_expenses_report_leaves_both_legs_out(
    session: AsyncSession, test_user, test_workspace, to_char
):
    home = await _household(session, test_user, test_workspace)
    today = date.today().replace(day=15)
    credit, debit = await _transfer_legs(session, home, test_user, test_workspace, today)
    await session.commit()

    async def totals() -> dict[str, float]:
        report = await report_service.get_income_expenses_report(
            session, test_workspace.id, test_user.id, months=1, interval="monthly",
            currency="USD",
        )
        return {b.key: b.value for b in report.summary.breakdowns}

    before = await totals()
    assert before["income"] == pytest.approx(2000.0, abs=0.01)
    assert before["expenses"] == pytest.approx(2000.0, abs=0.01)

    await _mark(session, home, test_workspace, credit, debit)

    after = await totals()
    assert after["income"] == 0.0
    assert after["expenses"] == 0.0


@pytest.mark.asyncio
async def test_the_cash_flow_baseline_does_not_average_the_transfer_in(
    session: AsyncSession, test_user, test_workspace
):
    """The baseline projects the future from the recent past. A
    contribution is not a flow the household earns or spends, so it must
    not raise the mean daily inflow."""
    home = await _household(session, test_user, test_workspace)
    today = date.today()
    when = today - timedelta(days=10)
    credit, debit = await _transfer_legs(session, home, test_user, test_workspace, when)
    await session.commit()

    async def to_primary(amount, currency):
        return float(amount)

    async def inflow_per_day() -> float:
        projections, _ = await report_service._get_baseline_projection(
            session, test_workspace.id, today, today + timedelta(days=5), "USD", to_primary
        )
        return sum(p["amount"] for p in projections if p["type"] == "credit")

    assert await inflow_per_day() > 0.0

    await _mark(session, home, test_workspace, credit, debit)

    assert await inflow_per_day() == 0.0


@pytest.mark.asyncio
async def test_a_budget_does_not_spend_the_transfer(
    session: AsyncSession, test_user, test_workspace
):
    """Her side of the transfer is a debit in a category. Once it is a
    contribution it is not spending, so the budget's actual drops back to
    the groceries alone."""
    home = await _household(session, test_user, test_workspace)
    today = date.today().replace(day=15)
    household = Category(
        id=uuid.uuid4(),
        workspace_id=test_workspace.id,
        user_id=test_user.id,
        name="Household",
    )
    session.add(household)
    await session.flush()

    credit, debit = await _transfer_legs(
        session, home, test_user, test_workspace, today, category_id=household.id
    )
    await _tx(
        session,
        test_user.id,
        test_workspace.id,
        home["mine"].id,
        GROCERIES,
        type_="debit",
        when=today,
        description="Groceries",
        category_id=household.id,
    )
    session.add(
        Budget(
            id=uuid.uuid4(),
            user_id=test_user.id,
            workspace_id=test_workspace.id,
            category_id=household.id,
            amount=Decimal("5000.00"),
            currency="USD",
            month=today.replace(day=1),
            is_recurring=True,
        )
    )
    await session.commit()

    async def actual() -> float:
        rows = await budget_service.get_budget_vs_actual(
            session, test_workspace.id, test_user.id, today.replace(day=1)
        )
        return float(next(r for r in rows if r.category_id == household.id).actual_amount)

    assert await actual() == pytest.approx(2300.0, abs=0.01)

    await _mark(session, home, test_workspace, credit, debit)

    assert await actual() == pytest.approx(300.0, abs=0.01)


@pytest.mark.asyncio
async def test_the_transaction_footer_totals_leave_the_transfer_out(
    session: AsyncSession, test_user, test_workspace
):
    """It leaves income and expense and turns up under `excluded`, the
    footer's own account of what it left out — and the rows are still in
    the list."""
    home = await _household(session, test_user, test_workspace)
    today = date.today().replace(day=15)
    credit, debit = await _transfer_legs(session, home, test_user, test_workspace, today)
    await session.commit()

    async def footer() -> tuple[dict, int]:
        _txs, total, summary = await transaction_service.get_transactions(
            session, test_workspace.id, test_user.id, include_summary=True
        )
        assert summary is not None
        return summary, total

    before, count_before = await footer()
    assert float(before["income"]) == pytest.approx(2000.0, abs=0.01)
    assert float(before["expense"]) == pytest.approx(2000.0, abs=0.01)

    await _mark(session, home, test_workspace, credit, debit)

    after, count_after = await footer()
    assert float(after["income"]) == 0.0
    assert float(after["expense"]) == 0.0
    assert float(after["excluded"]) == pytest.approx(4000.0, abs=0.01)
    assert count_after == count_before, "both rows stay in the transaction list"


@pytest.mark.asyncio
async def test_account_statistics_drop_the_transfer_but_the_balance_keeps_it(
    session: AsyncSession, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    today = date.today().replace(day=15)
    credit, debit = await _transfer_legs(session, home, test_user, test_workspace, today)
    await session.commit()

    async def summary(account_id) -> dict:
        result = await account_service.get_account_summary(
            session, account_id, test_workspace.id, today.replace(day=1), today
        )
        assert result is not None
        return result

    before = await summary(home["mine"].id)
    assert before["monthly_income"] == pytest.approx(2000.0, abs=0.01)
    assert before["current_balance"] == pytest.approx(2000.0, abs=0.01)

    await _mark(session, home, test_workspace, credit, debit)

    his = await summary(home["mine"].id)
    assert his["monthly_income"] == 0.0
    assert his["current_balance"] == pytest.approx(
        2000.0, abs=0.01
    ), "the money did land on the account"

    hers = await summary(home["hers"].id)
    assert hers["monthly_expenses"] == 0.0
    assert hers["current_balance"] == pytest.approx(-2000.0, abs=0.01)


@pytest.mark.asyncio
async def test_the_forecast_leaves_a_pending_transfer_out(
    session: AsyncSession, test_user, test_workspace
):
    """A pending row is forecast, not actual, and goes through the
    separate in-memory predicate rather than the SQL filter."""
    home = await _household(session, test_user, test_workspace)
    today = date.today().replace(day=15)
    credit, debit = await _transfer_legs(
        session, home, test_user, test_workspace, today, status="pending"
    )
    await session.commit()

    month = today.replace(day=1)

    async def projected() -> tuple[float, float]:
        summary = await dashboard_service.get_summary(
            session, test_workspace.id, test_user.id, month
        )
        return (
            summary.projected_income_primary,
            summary.projected_expenses_primary,
        )

    income_before, expense_before = await projected()
    assert income_before == pytest.approx(2000.0, abs=0.01)
    assert expense_before == pytest.approx(2000.0, abs=0.01)

    await _mark(session, home, test_workspace, credit, debit)

    income_after, expense_after = await projected()
    assert income_after == 0.0
    assert expense_after == 0.0


@pytest.mark.asyncio
async def test_a_contribution_still_moves_the_balance_and_shows_in_the_list(
    session: AsyncSession, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    today = date.today().replace(day=15)
    credit, debit = await _transfer_legs(session, home, test_user, test_workspace, today)
    await _mark(session, home, test_workspace, credit, debit)

    txs, total, _summary = await transaction_service.get_transactions(
        session, test_workspace.id, test_user.id, account_id=home["mine"].id
    )
    assert total == 1
    assert txs[0].id == credit.id

    for name, expected in (("mine", 2000.0), ("hers", -2000.0)):
        summary = await account_service.get_account_summary(
            session, home[name].id, test_workspace.id, today.replace(day=1), today
        )
        assert summary is not None
        assert summary["current_balance"] == pytest.approx(expected, abs=0.01)


@pytest.mark.asyncio
async def test_the_calendar_shows_no_income_on_the_day_of_a_contribution(
    session: AsyncSession, test_user, test_workspace
):
    """Booked rows. Dashboard and reports agree the transfer is neither
    income nor spending; the day grid has to say the same thing."""
    from app.services.transaction_calendar_service import get_transaction_calendar

    home = await _household(session, test_user, test_workspace)
    today = date.today().replace(day=5)
    credit, debit = await _transfer_legs(session, home, test_user, test_workspace, today)
    await session.commit()

    async def day() -> TransactionCalendarDay:
        calendar = await get_transaction_calendar(
            session, test_workspace.id, test_user.id, today.replace(day=1)
        )
        return next(d for d in calendar.days if d.date == today)

    before = await day()
    assert before.income == pytest.approx(2000.0, abs=0.01)
    assert before.expense == pytest.approx(2000.0, abs=0.01)

    await _mark(session, home, test_workspace, credit, debit)

    after = await day()
    assert after.income == 0.0
    assert after.expense == 0.0
    assert after.actual_income == 0.0
    assert after.actual_expense == 0.0
    # Still listed, and still moving the balance: the money did move.
    assert after.actual_count == 2
    assert {item.description for item in after.items} == {"Monthly transfer"}
    assert after.ending_balance == before.ending_balance


@pytest.mark.asyncio
async def test_the_calendar_leaves_a_pending_contribution_out_of_projected_income(
    session: AsyncSession, test_user, test_workspace
):
    """Forecast rows, which the calendar buckets from its own loader."""
    from app.services.transaction_calendar_service import get_transaction_calendar

    home = await _household(session, test_user, test_workspace)
    today = date.today().replace(day=5)
    credit, debit = await _transfer_legs(
        session, home, test_user, test_workspace, today, status="pending"
    )
    await session.commit()

    async def day() -> TransactionCalendarDay:
        calendar = await get_transaction_calendar(
            session, test_workspace.id, test_user.id, today.replace(day=1)
        )
        return next(d for d in calendar.days if d.date == today)

    before = await day()
    assert before.projected_income == pytest.approx(2000.0, abs=0.01)
    assert before.projected_expense == pytest.approx(2000.0, abs=0.01)

    await _mark(session, home, test_workspace, credit, debit)

    after = await day()
    assert after.projected_income == 0.0
    assert after.projected_expense == 0.0
    assert after.income == 0.0
    assert after.expense == 0.0
    # The projected walk still carries them: they will land.
    assert after.projected_count == 2
    assert after.ending_balance == before.ending_balance


@pytest.mark.asyncio
async def test_the_agents_aggregate_tool_leaves_the_transfer_out(
    session: AsyncSession, test_user, test_workspace
):
    """An agent asking "how much came in last month?" must give the same
    answer as the screen."""
    import mcp_server.tools.aggregate as aggregate_tool
    from mcp_server.auth import CallContext

    home = await _household(session, test_user, test_workspace)
    today = date.today().replace(day=15)
    credit, debit = await _transfer_legs(session, home, test_user, test_workspace, today)
    await _tx(
        session,
        test_user.id,
        test_workspace.id,
        home["mine"].id,
        GROCERIES,
        type_="debit",
        when=today,
        description="Groceries",
    )
    await session.commit()

    async def income() -> float:
        result = await aggregate_tool.aggregate(
            session=session,
            ctx=CallContext(user_id=test_user.id),
            metric="sum",
            group_by="month",
            tx_type="income",
        )
        return sum(item["value"] or 0 for item in result["items"])

    async def expense() -> float:
        result = await aggregate_tool.aggregate(
            session=session,
            ctx=CallContext(user_id=test_user.id),
            metric="sum",
            group_by="month",
            tx_type="expense",
        )
        return sum(item["value"] or 0 for item in result["items"])

    assert await income() == pytest.approx(2000.0, abs=0.01)
    assert await expense() == pytest.approx(2300.0, abs=0.01)

    await _mark(session, home, test_workspace, credit, debit)

    assert await income() == 0.0
    assert await expense() == pytest.approx(300.0, abs=0.01)


@pytest.mark.asyncio
async def test_a_payee_summary_does_not_count_the_transfer(
    session: AsyncSession, test_user, test_workspace
):
    """Her transfers are filed under a payee. They are neither money
    spent at that payee nor received from them."""
    from app.models.payee import Payee
    from app.services import payee_service

    home = await _household(session, test_user, test_workspace)
    today = date.today().replace(day=15)
    payee = Payee(
        id=uuid.uuid4(),
        workspace_id=test_workspace.id,
        user_id=test_user.id,
        name="Anna",
    )
    session.add(payee)
    await session.flush()

    credit, debit = await _transfer_legs(session, home, test_user, test_workspace, today)
    credit.payee_id = payee.id
    debit.payee_id = payee.id
    await session.commit()

    async def summary() -> dict:
        return await payee_service.get_payee_summary(
            session, payee.id, test_workspace.id
        )

    before = await summary()
    assert float(before["total_received"]) == pytest.approx(2000.0, abs=0.01)
    assert float(before["total_spent"]) == pytest.approx(2000.0, abs=0.01)

    await _mark(session, home, test_workspace, credit, debit)

    after = await summary()
    assert float(after["total_received"]) == 0.0
    assert float(after["total_spent"]) == 0.0
    # Still the payee's transactions, and still counted as such.
    assert after["payee"].transaction_count == 2
