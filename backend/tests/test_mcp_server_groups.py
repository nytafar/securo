"""Cover mcp_server/tools/groups.py — the three group/balance/settlement
tools exposed to the agent. Service-layer behaviour is already covered
in tests/test_groups.py; here we focus on the MCP serialization layer:
name resolution, error pass-through, decimal coercion, etc.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

import mcp_server.tools.groups as groups_tool
from mcp_server.auth import CallContext


def _ctx(user_id):
    return CallContext(user_id=user_id)


# --------------------------------------------------------------------- list_groups

@pytest.mark.asyncio
async def test_list_groups_serializes_group_with_members(session, test_user, monkeypatch):
    gid = uuid.uuid4()
    mid1 = uuid.uuid4()
    mid2 = uuid.uuid4()
    fake_group = SimpleNamespace(
        id=gid,
        name="Amigos",
        kind="expense",
        default_currency="BRL",
        is_archived=False,
        members=[
            SimpleNamespace(id=mid1, name="Tássio", is_self=True),
            SimpleNamespace(id=mid2, name="Bia", is_self=False),
        ],
    )

    async def fake_list_groups(s, ws_id, uid, *, include_archived):
        assert uid == test_user.id
        assert include_archived is True
        return [fake_group]

    monkeypatch.setattr(groups_tool.group_service, "list_groups", fake_list_groups)

    result = await groups_tool.list_groups(
        session=session, ctx=_ctx(test_user.id), include_archived=True
    )
    assert result["total"] == 1
    g = result["items"][0]
    assert g["id"] == str(gid)
    assert g["name"] == "Amigos"
    assert g["kind"] == "expense"
    assert g["default_currency"] == "BRL"
    assert g["is_archived"] is False
    assert len(g["members"]) == 2
    assert {m["name"] for m in g["members"]} == {"Tássio", "Bia"}
    assert next(m for m in g["members"] if m["name"] == "Tássio")["is_self"] is True


@pytest.mark.asyncio
async def test_list_groups_handles_missing_members(session, test_user, monkeypatch):
    """If a group has no members (or None), the tool must not crash."""
    fake_group = SimpleNamespace(
        id=uuid.uuid4(),
        name="Solo",
        kind="expense",
        default_currency="USD",
        is_archived=True,
        members=None,
    )

    async def fake(s, ws_id, uid, *, include_archived):
        return [fake_group]

    monkeypatch.setattr(groups_tool.group_service, "list_groups", fake)
    result = await groups_tool.list_groups(session=session, ctx=_ctx(test_user.id))
    assert result["items"][0]["members"] == []


@pytest.mark.asyncio
async def test_list_groups_defaults_include_archived_false(session, test_user, monkeypatch):
    captured = {}

    async def fake(s, ws_id, uid, *, include_archived):
        captured["include_archived"] = include_archived
        return []

    monkeypatch.setattr(groups_tool.group_service, "list_groups", fake)
    await groups_tool.list_groups(session=session, ctx=_ctx(test_user.id))
    assert captured["include_archived"] is False


# --------------------------------------------------------------------- get_group_balances

@pytest.mark.asyncio
async def test_get_group_balances_returns_error_when_group_missing(session, test_user, monkeypatch):
    async def fake_compute(s, gid, ws_id, uid):
        return None  # service signals not-visible/not-found via None
    monkeypatch.setattr(groups_tool.balance_service, "compute_balances", fake_compute)

    result = await groups_tool.get_group_balances(
        session=session, ctx=_ctx(test_user.id), group_id=str(uuid.uuid4())
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_get_group_balances_resolves_member_names(session, test_user, monkeypatch):
    """Happy path: compute_balances returns raw lines; the tool resolves
    member names and is_self flags from the GroupMember table."""
    from app.models.group import Group, GroupMember

    g = Group(
        id=uuid.uuid4(), user_id=test_user.id, name="Roomies",
        kind="expense", default_currency="USD", is_archived=False,
    )
    me = GroupMember(id=uuid.uuid4(), group_id=g.id, name="Me", is_self=True)
    them = GroupMember(id=uuid.uuid4(), group_id=g.id, name="Alex", is_self=False)
    session.add_all([g, me, them])
    await session.commit()

    async def fake_compute(s, gid, ws_id, uid):
        assert gid == g.id
        return {
            "group_id": g.id,
            "self_member_id": me.id,
            "default_currency": "USD",
            "lines": [
                {"member_id": them.id, "currency": "USD", "amount": Decimal("12.50"),
                 "amount_in_default_currency": Decimal("12.50")},
            ],
        }

    monkeypatch.setattr(groups_tool.balance_service, "compute_balances", fake_compute)

    result = await groups_tool.get_group_balances(
        session=session, ctx=_ctx(test_user.id), group_id=str(g.id)
    )
    assert result["group_id"] == str(g.id)
    assert result["self_member_id"] == str(me.id)
    assert result["default_currency"] == "USD"
    assert len(result["lines"]) == 1
    ln = result["lines"][0]
    assert ln["member_id"] == str(them.id)
    assert ln["member_name"] == "Alex"
    assert ln["is_self"] is False
    assert ln["amount"] == 12.5
    assert ln["amount_in_default_currency"] == 12.5


@pytest.mark.asyncio
async def test_get_group_balances_handles_none_self_member(session, test_user, monkeypatch):
    """If the user isn't a member, self_member_id comes back None — we
    must serialize that as null, not crash."""
    gid = uuid.uuid4()

    async def fake_compute(s, g, ws_id, u):
        return {
            "group_id": gid, "self_member_id": None, "default_currency": "BRL",
            "lines": [],
        }
    monkeypatch.setattr(groups_tool.balance_service, "compute_balances", fake_compute)
    result = await groups_tool.get_group_balances(
        session=session, ctx=_ctx(test_user.id), group_id=str(gid)
    )
    assert result["self_member_id"] is None


# --------------------------------------------------------------------- list_group_settlements

@pytest.mark.asyncio
async def test_list_group_settlements_returns_error_when_group_missing(session, test_user, monkeypatch):
    async def fake_list(s, gid, ws_id, uid):
        return None
    monkeypatch.setattr(groups_tool.settlement_service, "list_settlements", fake_list)

    result = await groups_tool.list_group_settlements(
        session=session, ctx=_ctx(test_user.id), group_id=str(uuid.uuid4())
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_list_group_settlements_serializes_rows(session, test_user, monkeypatch):
    s1 = SimpleNamespace(
        id=uuid.uuid4(),
        group_id=uuid.uuid4(),
        from_member_id=uuid.uuid4(),
        to_member_id=uuid.uuid4(),
        amount=Decimal("99.95"),
        currency="EUR",
        date=date(2026, 5, 1),
        notes="brunch",
        transaction_id=uuid.uuid4(),
        receiver_transaction_id=None,
    )
    s2 = SimpleNamespace(
        id=uuid.uuid4(),
        group_id=s1.group_id,
        from_member_id=uuid.uuid4(),
        to_member_id=uuid.uuid4(),
        amount=Decimal("10.00"),
        currency="EUR",
        date=None,  # exercise the None-date branch
        notes=None,
        transaction_id=None,  # exercise the None-txn branch
        receiver_transaction_id=None,
    )

    async def fake(s, gid, ws_id, uid):
        return [s1, s2]
    monkeypatch.setattr(groups_tool.settlement_service, "list_settlements", fake)

    result = await groups_tool.list_group_settlements(
        session=session, ctx=_ctx(test_user.id), group_id=str(s1.group_id)
    )
    assert result["total"] == 2
    first, second = result["items"]
    assert first["id"] == str(s1.id)
    assert first["amount"] == 99.95
    assert first["currency"] == "EUR"
    assert first["date"] == "2026-05-01"
    assert first["notes"] == "brunch"
    assert first["transaction_id"] == str(s1.transaction_id)
    # Both sides are exposed, so an agent can reconcile a contribution
    # against the bank without a second call.
    assert first["payer_transaction_id"] == str(s1.transaction_id)
    assert first["receiver_transaction_id"] is None
    assert second["date"] is None
    assert second["transaction_id"] is None
    assert second["payer_transaction_id"] is None
    assert second["receiver_transaction_id"] is None
    assert second["amount"] == 10.0


# ------------------------------------------------- propose_mark_contribution


@pytest.mark.asyncio
async def test_listed_contributions_show_a_legacy_credit_on_the_receiver_side(
    session, test_user, test_workspace
):
    """Real rows, not fakes: the owner's own settlements link the credit
    that landed on his account in the payer-side column."""
    from datetime import date as _date
    from app.models.group_settlement import GroupSettlement
    from tests.test_contributions import _household, _tx

    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00", type_="credit"
    )
    session.add(
        GroupSettlement(
            id=uuid.uuid4(),
            group_id=home["group"].id,
            workspace_id=test_workspace.id,
            from_member_id=home["partner"].id,
            to_member_id=home["me"].id,
            amount=Decimal("2000.00"),
            currency="USD",
            date=_date.today(),
            transaction_id=credit.id,
        )
    )
    await session.commit()

    result = await groups_tool.list_group_settlements(
        session=session, ctx=_ctx(test_user.id), group_id=str(home["group"].id)
    )
    (row,) = result["items"]
    assert row["payer_transaction_id"] is None
    assert row["receiver_transaction_id"] == str(credit.id)


@pytest.mark.asyncio
async def test_propose_mark_contribution_previews_the_side_it_read(
    session, test_user, test_workspace
):
    from mcp_server.auth import CallContext as _Ctx
    from mcp_server.registry import REGISTRY
    from tests.test_contributions import _household, _tx

    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00", type_="credit"
    )
    await session.commit()

    handler = REGISTRY["propose_mark_contribution"].handler
    result = await handler(
        session=session,
        ctx=_Ctx(user_id=test_user.id, external=False),
        group_id=str(home["group"].id),
        transaction_id=str(credit.id),
        member_id=str(home["partner"].id),
    )
    assert result["kind"] == "mark_contribution"
    assert result["proposed"]["side"] == "receiver"
    assert result["proposed"]["amount"] == 2000.0
    assert "applied" not in result


@pytest.mark.asyncio
async def test_propose_mark_contribution_does_not_write_for_an_internal_caller(
    session, test_user, test_workspace
):
    from sqlalchemy import select as _select

    from app.models.group_settlement import GroupSettlement
    from mcp_server.auth import CallContext as _Ctx
    from mcp_server.registry import REGISTRY
    from tests.test_contributions import _household, _tx

    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00", type_="credit"
    )
    await session.commit()

    handler = REGISTRY["propose_mark_contribution"].handler
    result = await handler(
        session=session,
        ctx=_Ctx(user_id=test_user.id, external=False),
        group_id=str(home["group"].id),
        transaction_id=str(credit.id),
        member_id=str(home["partner"].id),
        apply=True,
    )
    assert "applied" not in result
    assert (await session.execute(_select(GroupSettlement.id))).first() is None


@pytest.mark.asyncio
async def test_propose_mark_contribution_writes_for_an_external_caller_that_applies(
    session, test_user, test_workspace
):
    from sqlalchemy import select as _select

    from app.models.group_settlement import GroupSettlement
    from mcp_server.auth import CallContext as _Ctx
    from mcp_server.registry import REGISTRY
    from tests.test_contributions import _household, _tx

    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00", type_="credit"
    )
    await session.commit()

    handler = REGISTRY["propose_mark_contribution"].handler
    result = await handler(
        session=session,
        ctx=_Ctx(user_id=test_user.id, external=True),
        group_id=str(home["group"].id),
        transaction_id=str(credit.id),
        member_id=str(home["partner"].id),
        apply=True,
    )
    assert result.get("applied") is True

    row = (await session.execute(_select(GroupSettlement))).scalars().one()
    assert row.receiver_transaction_id == credit.id
    assert row.from_member_id == home["partner"].id


@pytest.mark.asyncio
async def test_propose_mark_contribution_refuses_a_counterparty_outside_the_group(
    session, test_user, test_workspace
):
    from mcp_server.auth import CallContext as _Ctx
    from mcp_server.registry import REGISTRY
    from tests.test_contributions import _household, _tx

    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00", type_="credit"
    )
    await session.commit()

    handler = REGISTRY["propose_mark_contribution"].handler
    result = await handler(
        session=session,
        ctx=_Ctx(user_id=test_user.id, external=True),
        group_id=str(home["group"].id),
        transaction_id=str(credit.id),
        member_id=str(uuid.uuid4()),
        apply=True,
    )
    assert "must belong to the group" in result["error"]


@pytest.mark.asyncio
async def test_propose_mark_contribution_refuses_in_the_preview_what_apply_would_refuse(
    session, test_user, test_workspace
):
    """The tool's description promises it turns down a transaction that
    is already a contribution or carries shares. A preview that promised
    otherwise would send the user to an Apply that fails."""
    from app.schemas.transaction_split import (
        TransactionSplitInput,
        TransactionSplitsInput,
    )
    from app.services import settlement_service, split_service
    from mcp_server.auth import CallContext as _Ctx
    from mcp_server.registry import REGISTRY
    from tests.test_contributions import _household, _tx

    home = await _household(session, test_user, test_workspace)
    already = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00", type_="credit"
    )
    shared = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "300.00", type_="credit"
    )
    await split_service.replace_splits(
        session,
        shared,
        TransactionSplitsInput(
            share_type="equal",
            splits=[
                TransactionSplitInput(group_member_id=home["me"].id),
                TransactionSplitInput(group_member_id=home["partner"].id),
            ],
        ),
        test_user.id,
    )
    await session.commit()

    from app.schemas.group_settlement import MarkContributionFromTransaction

    await settlement_service.mark_transaction_as_contribution(
        session,
        home["group"].id,
        test_workspace.id,
        test_user.id,
        MarkContributionFromTransaction(
            transaction_id=already.id, member_id=home["partner"].id
        ),
    )

    handler = REGISTRY["propose_mark_contribution"].handler
    ctx = _Ctx(user_id=test_user.id, external=False)

    linked = await handler(
        session=session,
        ctx=ctx,
        group_id=str(home["group"].id),
        transaction_id=str(already.id),
        member_id=str(home["partner"].id),
    )
    assert "already linked" in linked["error"]
    assert "proposed" not in linked

    carries_shares = await handler(
        session=session,
        ctx=ctx,
        group_id=str(home["group"].id),
        transaction_id=str(shared.id),
        member_id=str(home["partner"].id),
    )
    assert "shared in a group" in carries_shares["error"]
    assert "proposed" not in carries_shares


@pytest.mark.asyncio
async def test_propose_mark_contribution_shows_the_date_that_will_be_stored(
    session, test_user, test_workspace
):
    """A card row is bucketed by its bill date, and that is the date the
    contribution carries — not the transaction's own."""
    from datetime import date as _date

    from mcp_server.auth import CallContext as _Ctx
    from mcp_server.registry import REGISTRY
    from tests.test_contributions import _household, _tx

    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session,
        test_user.id,
        test_workspace.id,
        home["mine"].id,
        "2000.00",
        type_="credit",
        when=_date(2026, 5, 20),
    )
    credit.effective_bill_date = _date(2026, 6, 10)
    await session.commit()

    handler = REGISTRY["propose_mark_contribution"].handler
    preview = await handler(
        session=session,
        ctx=_Ctx(user_id=test_user.id, external=False),
        group_id=str(home["group"].id),
        transaction_id=str(credit.id),
        member_id=str(home["partner"].id),
    )
    assert preview["proposed"]["date"] == "2026-06-10"
    assert preview["outcome"] == "create"

    applied = await handler(
        session=session,
        ctx=_Ctx(user_id=test_user.id, external=True),
        group_id=str(home["group"].id),
        transaction_id=str(credit.id),
        member_id=str(home["partner"].id),
        apply=True,
    )
    assert applied.get("applied") is True

    from app.models.group_settlement import GroupSettlement as _Settlement
    from sqlalchemy import select as _select

    row = (await session.execute(_select(_Settlement))).scalars().one()
    assert row.date == _date(2026, 6, 10)


@pytest.mark.asyncio
async def test_propose_mark_contribution_says_when_it_will_attach_to_the_other_leg(
    session, test_user, test_workspace
):
    from datetime import date as _date

    from app.schemas.group_settlement import MarkContributionFromTransaction
    from app.services import settlement_service
    from mcp_server.auth import CallContext as _Ctx
    from mcp_server.registry import REGISTRY
    from tests.test_contributions import _household, _tx

    home = await _household(session, test_user, test_workspace)
    when = _date(2026, 5, 20)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00",
        type_="credit", when=when,
    )
    debit = await _tx(
        session, home["partner_user"].id, test_workspace.id, home["hers"].id, "2000.00",
        when=when,
    )
    await session.commit()

    first = await settlement_service.mark_transaction_as_contribution(
        session,
        home["group"].id,
        test_workspace.id,
        test_user.id,
        MarkContributionFromTransaction(
            transaction_id=credit.id, member_id=home["partner"].id
        ),
    )
    assert first is not None

    handler = REGISTRY["propose_mark_contribution"].handler
    preview = await handler(
        session=session,
        ctx=_Ctx(user_id=test_user.id, external=False),
        group_id=str(home["group"].id),
        transaction_id=str(debit.id),
        member_id=str(home["me"].id),
    )
    assert preview["outcome"] == "attach"
    assert preview["existing_settlement_id"] == str(first.id)
    assert preview["proposed"]["side"] == "payer"


@pytest.mark.asyncio
async def test_an_attach_preview_shows_the_date_and_notes_that_will_stand(
    session, test_user, test_workspace
):
    """Attaching keeps the contribution's own date and fills its notes
    only when it has none. A preview showing the incoming values would
    promise an edit the Apply does not make."""
    from datetime import date as _date

    from app.schemas.group_settlement import MarkContributionFromTransaction
    from app.services import settlement_service
    from mcp_server.auth import CallContext as _Ctx
    from mcp_server.registry import REGISTRY
    from tests.test_contributions import _household, _tx

    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00",
        type_="credit", when=_date(2026, 5, 20),
    )
    # One day later, and inside the window, so the two are one transfer.
    debit = await _tx(
        session, home["partner_user"].id, test_workspace.id, home["hers"].id, "2000.00",
        when=_date(2026, 5, 21),
    )
    await session.commit()

    first = await settlement_service.mark_transaction_as_contribution(
        session,
        home["group"].id,
        test_workspace.id,
        test_user.id,
        MarkContributionFromTransaction(
            transaction_id=credit.id, member_id=home["partner"].id, notes="May"
        ),
    )
    assert first is not None

    handler = REGISTRY["propose_mark_contribution"].handler
    preview = await handler(
        session=session,
        ctx=_Ctx(user_id=test_user.id, external=False),
        group_id=str(home["group"].id),
        transaction_id=str(debit.id),
        member_id=str(home["me"].id),
        notes="a different note",
    )
    assert preview["outcome"] == "attach"
    assert preview["proposed"]["date"] == "2026-05-20"
    assert preview["proposed"]["notes"] == "May"

    applied = await handler(
        session=session,
        ctx=_Ctx(user_id=test_user.id, external=True),
        group_id=str(home["group"].id),
        transaction_id=str(debit.id),
        member_id=str(home["me"].id),
        notes="a different note",
        apply=True,
    )
    assert applied.get("applied") is True

    await session.refresh(first)
    assert first.date == _date(2026, 5, 20)
    assert first.notes == "May"


@pytest.mark.asyncio
async def test_an_attach_preview_offers_the_incoming_note_when_there_is_none(
    session, test_user, test_workspace
):
    from datetime import date as _date

    from app.schemas.group_settlement import MarkContributionFromTransaction
    from app.services import settlement_service
    from mcp_server.auth import CallContext as _Ctx
    from mcp_server.registry import REGISTRY
    from tests.test_contributions import _household, _tx

    home = await _household(session, test_user, test_workspace)
    when = _date(2026, 5, 20)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00",
        type_="credit", when=when,
    )
    debit = await _tx(
        session, home["partner_user"].id, test_workspace.id, home["hers"].id, "2000.00",
        when=when,
    )
    await session.commit()

    await settlement_service.mark_transaction_as_contribution(
        session,
        home["group"].id,
        test_workspace.id,
        test_user.id,
        MarkContributionFromTransaction(
            transaction_id=credit.id, member_id=home["partner"].id
        ),
    )

    handler = REGISTRY["propose_mark_contribution"].handler
    preview = await handler(
        session=session,
        ctx=_Ctx(user_id=test_user.id, external=False),
        group_id=str(home["group"].id),
        transaction_id=str(debit.id),
        member_id=str(home["me"].id),
        notes="May",
    )
    assert preview["outcome"] == "attach"
    assert preview["proposed"]["notes"] == "May"
