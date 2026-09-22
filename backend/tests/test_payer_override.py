"""The payer override: who really paid a shared transaction.

The payer is normally the owner of the account a transaction sits on.
That is wrong for cash, for an account outside the app and for a member
with no Securo user, so a transaction's group sharing may name its payer
explicitly. Money facts in, money outcomes out: the same positions the
group page shows.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient

from app.schemas.transaction_split import TransactionSplitInput, TransactionSplitsInput
from app.services import group_service, position_service, split_service
from tests.test_position_service import (
    _assert_sums_to_zero,
    _by_member,
    _household,
    _make_account,
    _make_tx,
    _make_user,
    _member,
    _positions,
    _share,
)

D = Decimal


async def _share_paid_by(session, tx, user_id, members, payer=None):
    """Share equally, naming the payer (or clearing the name when
    `payer` is None — the payload replaces the sharing wholesale)."""
    await split_service.replace_splits(
        session,
        tx,
        TransactionSplitsInput(
            share_type="equal",
            splits=[TransactionSplitInput(group_member_id=m.id) for m in members],
            payer_group_member_id=payer.id if payer is not None else None,
        ),
        user_id,
    )
    await session.commit()


# ───────────────────────── positions ──────────────────────────


@pytest.mark.asyncio
async def test_an_explicit_payer_wins_over_the_account_owner(
    session, test_user, test_workspace
):
    """She paid the 1 000 in cash and it landed on his account as a
    withdrawal: naming her turns the positions round."""
    group, me, her, _ = await _household(session, test_user, test_workspace.id)
    my_account = await _make_account(session, test_user.id, test_workspace.id)
    tx = await _make_tx(session, my_account, "1000.00")

    await _share(session, tx, test_user.id, [me, her])
    derived = await _positions(session, group, test_workspace.id, test_user.id)
    assert _by_member(derived, "paid") == {me.id: D("1000.00"), her.id: D("0")}
    assert _by_member(derived) == {me.id: D("-500.00"), her.id: D("500.00")}

    await _share_paid_by(session, tx, test_user.id, [me, her], payer=her)
    overridden = await _positions(session, group, test_workspace.id, test_user.id)
    assert _by_member(overridden, "paid") == {me.id: D("0"), her.id: D("1000.00")}
    assert _by_member(overridden) == {me.id: D("500.00"), her.id: D("-500.00")}
    assert _by_member(overridden, "share") == {me.id: D("500.00"), her.id: D("500.00")}
    _assert_sums_to_zero(overridden)


@pytest.mark.asyncio
async def test_clearing_the_override_restores_the_derived_payer(
    session, test_user, test_workspace
):
    group, me, her, _ = await _household(session, test_user, test_workspace.id)
    my_account = await _make_account(session, test_user.id, test_workspace.id)
    tx = await _make_tx(session, my_account, "1000.00")

    await _share_paid_by(session, tx, test_user.id, [me, her], payer=her)
    await _share_paid_by(session, tx, test_user.id, [me, her], payer=None)

    result = await _positions(session, group, test_workspace.id, test_user.id)
    assert _by_member(result, "paid") == {me.id: D("1000.00"), her.id: D("0")}
    assert _by_member(result) == {me.id: D("-500.00"), her.id: D("500.00")}


@pytest.mark.asyncio
async def test_a_member_with_no_securo_user_can_be_the_payer(
    session, test_user, test_workspace
):
    """The case the override exists for: she has no login, so nothing
    about the account can ever name her."""
    group, me, her, partner_user = await _household(
        session, test_user, test_workspace.id, partner_email=None
    )
    assert partner_user is None
    my_account = await _make_account(session, test_user.id, test_workspace.id)
    tx = await _make_tx(session, my_account, "600.00")

    await _share_paid_by(session, tx, test_user.id, [me, her], payer=her)

    result = await _positions(session, group, test_workspace.id, test_user.id)
    assert _by_member(result, "paid") == {me.id: D("0"), her.id: D("600.00")}
    assert _by_member(result) == {me.id: D("300.00"), her.id: D("-300.00")}
    _assert_sums_to_zero(result)


@pytest.mark.asyncio
async def test_the_payer_is_no_longer_assumed_once_it_is_typed_in(
    session, test_user, test_workspace
):
    """A transaction on an account nobody in the group owns is flagged
    payer-assumed. Naming the payer answers exactly that question."""
    group, me, her, _ = await _household(session, test_user, test_workspace.id)
    stranger = await _make_user(session, "stranger@example.com", test_workspace.id)
    outside = await _make_account(session, stranger.id, test_workspace.id)
    tx = await _make_tx(session, outside, "200.00")

    await _share(session, tx, test_user.id, [me, her])
    assumed = await position_service.compute_period(
        session, group.id, test_workspace.id, test_user.id
    )
    assert assumed is not None
    assert [t.id for t in assumed.payer_assumed_transactions] == [tx.id]
    assert assumed.transactions.items[0].payer_member_id == me.id
    assert assumed.transactions.items[0].payer_assumed is True

    await _share_paid_by(session, tx, test_user.id, [me, her], payer=her)
    named = await position_service.compute_period(
        session, group.id, test_workspace.id, test_user.id
    )
    assert named is not None
    assert named.payer_assumed_transactions == []
    assert named.transactions.items[0].payer_member_id == her.id
    assert named.transactions.items[0].payer_assumed is False


@pytest.mark.asyncio
async def test_the_payer_must_be_a_member_of_the_same_group(
    session, test_user, test_workspace
):
    from app.schemas.group import GroupCreate

    group, me, her, _ = await _household(session, test_user, test_workspace.id)
    other = await group_service.create_group(
        session, test_workspace.id, test_user.id, GroupCreate(name="Trip", kind="social")
    )
    outsider = await _member(session, other, test_workspace.id, name="Friend")
    my_account = await _make_account(session, test_user.id, test_workspace.id)
    tx = await _make_tx(session, my_account, "100.00")

    with pytest.raises(ValueError, match="member of the same group"):
        await _share_paid_by(session, tx, test_user.id, [me, her], payer=outsider)


@pytest.mark.asyncio
async def test_a_payer_with_no_shares_is_refused(session, test_user, test_workspace):
    """Clearing the sharing clears the payer with it, so a payload that
    names a payer and shares nothing is a mistake, not an override: say
    so rather than dropping the name on the floor."""
    _group, me, her, _ = await _household(session, test_user, test_workspace.id)
    my_account = await _make_account(session, test_user.id, test_workspace.id)
    tx = await _make_tx(session, my_account, "100.00")
    await _share_paid_by(session, tx, test_user.id, [me, her], payer=her)

    with pytest.raises(ValueError, match="payer without shares"):
        await split_service.replace_splits(
            session,
            tx,
            TransactionSplitsInput(
                share_type="equal", splits=[], payer_group_member_id=her.id
            ),
            test_user.id,
        )
    await session.rollback()

    # And clearing both together still works — that is how sharing ends.
    await _share_paid_by(session, tx, test_user.id, [])
    result = await _positions(session, _group, test_workspace.id, test_user.id)
    assert _by_member(result, "share") == {me.id: D("0"), her.id: D("0")}


@pytest.mark.asyncio
async def test_a_payer_who_is_gone_from_the_group_cannot_be_invented(
    session, test_user, test_workspace
):
    """Defence in depth for a column that outlived its member somehow:
    positions fall back to the derivation rather than crediting an id
    that is not a member of this group, and say the payer was assumed —
    the derived name is a guess, not what anybody typed in."""
    group, me, her, _ = await _household(session, test_user, test_workspace.id)
    my_account = await _make_account(session, test_user.id, test_workspace.id)
    tx = await _make_tx(session, my_account, "100.00")
    await _share(session, tx, test_user.id, [me, her])

    identity = await position_service._load_identity_for_group(session, group.id)
    assert identity is not None
    assert position_service._resolve_payer(identity, test_user.id, uuid.uuid4()) == (
        me.id,
        True,
    )
    # A payer who is still a member is not a guess.
    assert position_service._resolve_payer(identity, test_user.id, her.id) == (
        her.id,
        False,
    )


# ────────────────────── removing the payer ────────────────────


@pytest.mark.asyncio
async def test_removing_a_member_who_is_the_payer_is_refused(
    session, test_user, test_workspace
):
    """Never silently reassigned: moving the payment to somebody else
    would change what everyone owes without anyone asking."""
    group, me, her, _ = await _household(session, test_user, test_workspace.id)
    my_account = await _make_account(session, test_user.id, test_workspace.id)
    tx = await _make_tx(session, my_account, "100.00")
    # Only `me` carries a share, so the member's own RESTRICT is not what
    # refuses the removal here.
    await _share_paid_by(session, tx, test_user.id, [me], payer=her)

    with pytest.raises(ValueError, match="payer of 1 transaction"):
        await group_service.delete_member(session, group.id, her.id, test_workspace.id)

    # She paid the whole 100 and carries none of it, so the pot owes her
    # all of it and he owes the pot his share. Nothing leaks.
    kept = await _positions(session, group, test_workspace.id, test_user.id)
    assert _by_member(kept, "paid") == {me.id: D("0"), her.id: D("100.00")}
    assert _by_member(kept, "share") == {me.id: D("100.00"), her.id: D("0")}
    assert _by_member(kept) == {me.id: D("100.00"), her.id: D("-100.00")}
    _assert_sums_to_zero(kept)

    # Clearing the override frees her.
    await _share_paid_by(session, tx, test_user.id, [me], payer=None)
    assert await group_service.delete_member(
        session, group.id, her.id, test_workspace.id
    )


@pytest.mark.asyncio
async def test_removing_the_group_while_a_member_is_the_payer_is_refused(
    session, test_user, test_workspace
):
    """The same refusal one level up. PostgreSQL's RESTRICT would catch
    the cascade, but the suite runs on SQLite with foreign keys off, and
    a message that says which transactions are in the way beats one that
    says a constraint failed."""
    group, me, her, _ = await _household(session, test_user, test_workspace.id)
    my_account = await _make_account(session, test_user.id, test_workspace.id)
    tx = await _make_tx(session, my_account, "100.00")
    await _share_paid_by(session, tx, test_user.id, [me], payer=her)

    with pytest.raises(ValueError, match="payer of 1 transaction"):
        await group_service.delete_group(session, group.id, test_workspace.id)

    assert await position_service.compute_positions(
        session, group.id, test_workspace.id, test_user.id
    ) is not None

    # Clearing the override frees the group.
    await _share_paid_by(session, tx, test_user.id, [me], payer=None)
    assert await group_service.delete_group(session, group.id, test_workspace.id)


@pytest.mark.asyncio
async def test_removing_the_payer_over_the_api_is_a_conflict(
    client: AsyncClient, auth_headers
):
    group, me, her, tx_id = await _seed_over_the_api(client, auth_headers)

    resp = await client.delete(
        f"/api/groups/{group['id']}/members/{her['id']}", headers=auth_headers
    )
    assert resp.status_code == 409, resp.text
    assert "payer" in resp.json()["detail"]


# ─────────────────────── the API round trip ───────────────────


async def _seed_over_the_api(client: AsyncClient, auth_headers):
    """His account, her name on the payment: the household's cash case,
    set the way the app sets it."""
    account = (
        await client.post(
            "/api/accounts",
            headers=auth_headers,
            json={"name": "Mine", "type": "checking", "balance": 0, "currency": "USD"},
        )
    ).json()
    group = (
        await client.post(
            "/api/groups",
            headers=auth_headers,
            json={"name": "Home", "kind": "household", "default_currency": "USD"},
        )
    ).json()
    members = []
    for name in ("Me", "Partner"):
        resp = await client.post(
            f"/api/groups/{group['id']}/members",
            headers=auth_headers,
            json={"name": name, "is_self": name == "Me"},
        )
        assert resp.status_code == 201, resp.text
        members.append(resp.json())
    me, her = members

    resp = await client.post(
        "/api/transactions",
        headers=auth_headers,
        json={
            "account_id": account["id"],
            "description": "Market, her cash",
            "amount": 400,
            "date": "2026-05-04",
            "type": "debit",
            "currency": "USD",
            "splits": {
                "share_type": "equal",
                "splits": [{"group_member_id": m["id"]} for m in members],
                "payer_group_member_id": her["id"],
            },
        },
    )
    assert resp.status_code == 201, resp.text
    return group, me, her, resp.json()["id"]


async def _period(client, auth_headers, group_id) -> dict:
    resp = await client.get(
        f"/api/groups/{group_id}/period",
        headers=auth_headers,
        params={"start": "2026-05-01", "end": "2026-06-01"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_the_payer_is_set_and_cleared_on_the_transactions_group_sharing(
    client: AsyncClient, auth_headers
):
    group, me, her, tx_id = await _seed_over_the_api(client, auth_headers)

    period = await _period(client, auth_headers, group["id"])
    row = period["transactions"]["items"][0]
    assert row["payer_member_id"] == her["id"]
    assert row["payer_assumed"] is False
    assert period["payer_assumed_transactions"] == []
    paid = {p["member_id"]: D(p["paid"]) for p in period["positions"]}
    assert paid[her["id"]] == D("400.00")
    assert paid[me["id"]] == D("0")

    # The transaction read carries it back, which is what the edit
    # dialog opens on.
    read = (await client.get(f"/api/transactions/{tx_id}", headers=auth_headers)).json()
    assert {s["payer_group_member_id"] for s in read["splits"]} == {her["id"]}

    # Sharing again without a payer clears the override.
    resp = await client.patch(
        f"/api/transactions/{tx_id}",
        headers=auth_headers,
        json={
            "splits": {
                "share_type": "equal",
                "splits": [
                    {"group_member_id": me["id"]},
                    {"group_member_id": her["id"]},
                ],
            }
        },
    )
    assert resp.status_code == 200, resp.text

    cleared = await _period(client, auth_headers, group["id"])
    row = cleared["transactions"]["items"][0]
    assert row["payer_member_id"] == me["id"]
    assert row["payer_assumed"] is False
    paid = {p["member_id"]: D(p["paid"]) for p in cleared["positions"]}
    assert paid[me["id"]] == D("400.00")
    assert paid[her["id"]] == D("0")


@pytest.mark.asyncio
async def test_the_api_refuses_a_payer_from_another_group(
    client: AsyncClient, auth_headers
):
    group, me, her, tx_id = await _seed_over_the_api(client, auth_headers)
    other = (
        await client.post(
            "/api/groups",
            headers=auth_headers,
            json={"name": "Trip", "kind": "social", "default_currency": "USD"},
        )
    ).json()
    friend = (
        await client.post(
            f"/api/groups/{other['id']}/members",
            headers=auth_headers,
            json={"name": "Friend"},
        )
    ).json()

    resp = await client.patch(
        f"/api/transactions/{tx_id}",
        headers=auth_headers,
        json={
            "splits": {
                "share_type": "equal",
                "splits": [
                    {"group_member_id": me["id"]},
                    {"group_member_id": her["id"]},
                ],
                "payer_group_member_id": friend["id"],
            }
        },
    )
    assert resp.status_code == 400, resp.text

    # And the sharing that was there is untouched.
    period = await _period(client, auth_headers, group["id"])
    assert period["transactions"]["items"][0]["payer_member_id"] == her["id"]


# ──────────────────────────── MCP ─────────────────────────────


@pytest.mark.asyncio
async def test_the_share_proposal_names_the_payer_and_applies_it(
    session, test_user, test_workspace
):
    import mcp_server.tools.proposals  # noqa: F401  (registers the tool)
    from mcp_server.auth import CallContext as _Ctx
    from mcp_server.registry import REGISTRY
    from tests.test_contributions import _household as _home
    from tests.test_contributions import _tx

    home = await _home(session, test_user, test_workspace)
    groceries = await _tx(
        session,
        test_user.id,
        test_workspace.id,
        home["mine"].id,
        "300.00",
        description="SUPERMARKET",
    )
    await session.commit()

    handler = REGISTRY["propose_share_transaction"].handler
    preview = await handler(
        session=session,
        ctx=_Ctx(user_id=test_user.id, external=False),
        group_id=str(home["group"].id),
        transaction_id=str(groceries.id),
        payer_group_member_id=str(home["partner"].id),
    )
    assert preview["proposed"]["payer_group_member_id"] == str(home["partner"].id)
    assert preview["proposed"]["payer_member_name"] == "Partner"
    assert "applied" not in preview

    applied = await handler(
        session=session,
        ctx=_Ctx(user_id=test_user.id, external=True),
        group_id=str(home["group"].id),
        transaction_id=str(groceries.id),
        payer_group_member_id=str(home["partner"].id),
        apply=True,
    )
    assert applied["applied"] is True

    positions = await position_service.compute_positions(
        session, home["group"].id, test_workspace.id, test_user.id
    )
    assert positions is not None
    paid = {p.member_id: p.paid for p in positions.positions}
    assert paid[home["partner"].id] == D("300.00")
    assert paid[home["me"].id] == D("0")


@pytest.mark.asyncio
async def test_the_share_proposal_refuses_a_payer_from_another_group(
    session, test_user, test_workspace
):
    from app.schemas.group import GroupCreate
    import mcp_server.tools.proposals  # noqa: F401  (registers the tool)
    from mcp_server.auth import CallContext as _Ctx
    from mcp_server.registry import REGISTRY
    from tests.test_contributions import _household as _home
    from tests.test_contributions import _tx

    home = await _home(session, test_user, test_workspace)
    other = await group_service.create_group(
        session, test_workspace.id, test_user.id, GroupCreate(name="Trip", kind="social")
    )
    friend = await _member(session, other, test_workspace.id, name="Friend")
    groceries = await _tx(
        session,
        test_user.id,
        test_workspace.id,
        home["mine"].id,
        "300.00",
        description="SUPERMARKET",
    )
    await session.commit()

    handler = REGISTRY["propose_share_transaction"].handler
    result = await handler(
        session=session,
        ctx=_Ctx(user_id=test_user.id, external=True),
        group_id=str(home["group"].id),
        transaction_id=str(groceries.id),
        payer_group_member_id=str(friend.id),
        apply=True,
    )
    assert "member of the same group" in result["error"]


# ─────────────────── the migration, on a real table ───────────


def _load_migration():
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "092_transaction_split_payer.py"
    )
    spec = importlib.util.spec_from_file_location("migration_092", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _books_before_092():
    """`transaction_splits` as it stood before the override, with a row
    of real sharing in it."""
    import sqlalchemy as sa

    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE group_members (id TEXT PRIMARY KEY)")
        connection.exec_driver_sql(
            """
            CREATE TABLE transaction_splits (
                id TEXT PRIMARY KEY,
                transaction_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                group_member_id TEXT NOT NULL REFERENCES group_members (id),
                share_amount NUMERIC(15, 2) NOT NULL,
                share_type VARCHAR(10) NOT NULL DEFAULT 'exact',
                share_pct NUMERIC(5, 2),
                notes VARCHAR(500),
                created_at TIMESTAMP
            )
            """
        )
        connection.exec_driver_sql("INSERT INTO group_members VALUES ('m1')")
        connection.exec_driver_sql(
            "INSERT INTO transaction_splits VALUES "
            "('s1', 'tx1', 'ws1', 'm1', 150.00, 'equal', NULL, NULL, NULL)"
        )
    return engine


def _columns(connection) -> set[str]:
    import sqlalchemy as sa

    return {c["name"] for c in sa.inspect(connection).get_columns("transaction_splits")}


def test_the_migration_adds_a_nullable_payer_and_takes_it_away_again(monkeypatch):
    """Additive and nullable: the sharing that was there keeps its
    amount and derives its payer, as it did before. Exercised on SQLite;
    the same DDL is what PostgreSQL gets."""
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = _load_migration()
    engine = _books_before_092()

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)

        assert "payer_group_member_id" not in _columns(connection)
        migration.upgrade()

        assert "payer_group_member_id" in _columns(connection)
        assert "ix_transaction_splits_payer_group_member_id" in {
            i["name"] for i in sa.inspect(connection).get_indexes("transaction_splits")
        }
        assert {
            fk["name"]: fk["referred_table"]
            for fk in sa.inspect(connection).get_foreign_keys("transaction_splits")
        }.get("fk_transaction_splits_payer_group_member_id") == "group_members"

        row = connection.exec_driver_sql(
            "SELECT share_amount, payer_group_member_id FROM transaction_splits"
        ).one()
        assert Decimal(str(row[0])) == Decimal("150.00")
        assert row[1] is None

        # The override is writable, and stays optional.
        connection.exec_driver_sql(
            "INSERT INTO transaction_splits VALUES "
            "('s2', 'tx2', 'ws1', 'm1', 50.00, 'equal', NULL, NULL, NULL, 'm1')"
        )

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)

        migration.downgrade()
        assert "payer_group_member_id" not in _columns(connection)
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM transaction_splits"
        ).scalar() == 2


def test_the_migration_is_chained_to_the_previous_head():
    migration = _load_migration()
    assert migration.revision == "092"
    assert migration.down_revision == "091"


def test_todays_shares_keep_meaning_what_they_meant():
    """A guard on the promise the migration makes to the owner's books:
    a share row written before the override derives its payer, because
    the column it does not carry is the one that means 'derive it'."""
    assert (
        TransactionSplitsInput(
            share_type="equal", splits=[TransactionSplitInput(group_member_id=uuid.uuid4())]
        ).payer_group_member_id
        is None
    )


@pytest.mark.asyncio
async def test_a_share_written_without_a_payer_stores_null(
    session, test_user, test_workspace
):
    from sqlalchemy import select

    from app.models.transaction_split import TransactionSplit

    _group, me, her, _ = await _household(session, test_user, test_workspace.id)
    account = await _make_account(session, test_user.id, test_workspace.id)
    tx = await _make_tx(session, account, "100.00", when=date(2026, 5, 2))
    await _share(session, tx, test_user.id, [me, her])

    rows = (
        await session.execute(
            select(TransactionSplit).where(TransactionSplit.transaction_id == tx.id)
        )
    ).scalars().all()
    assert len(rows) == 2
    assert all(r.payer_group_member_id is None for r in rows)
