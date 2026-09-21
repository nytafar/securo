"""Contributions made of real bank transactions.

A contribution is money a member moved to carry their part of the common
pot. It is never a copy of a transaction: the real bank row is linked, on
the payer side or the receiver side. These tests state money facts and
bank rows and assert what the API makes of them — that one transaction
belongs to one contribution, that a shared transaction and a contribution
are two different things, that marking reads the side off the transaction
itself, that nothing synthetic appears unasked, and that a settlement
recorded before the receiver side existed is read on the right side with
its row untouched.
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
import sqlalchemy.exc
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.group_settlement import GroupSettlement
from app.models.transaction import Transaction
from app.models.user import User
from app.models.workspace import WorkspaceMember
from app.schemas.group import GroupCreate, GroupMemberCreate
from app.schemas.transaction_split import TransactionSplitInput, TransactionSplitsInput
from app.services import group_service, settlement_service, split_service


# ──────────────────────────── the household ─────────────────────────────


PARTNER_PASSWORD = "testpass123"


async def _partner(session: AsyncSession, workspace) -> User:
    """A second user in the same workspace, as both members of the
    household have had since their accounts landed in one place."""
    import bcrypt

    user = User(
        id=uuid.uuid4(),
        email=f"partner-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password=bcrypt.hashpw(
            PARTNER_PASSWORD.encode(), bcrypt.gensalt()
        ).decode(),
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
            # Both members keep their own books in the shared workspace,
            # so she writes there too.
            role="editor",
        )
    )
    await session.flush()
    return user


async def _account(session: AsyncSession, user_id, workspace_id, name="Wallet") -> Account:
    account = Account(
        id=uuid.uuid4(),
        user_id=user_id,
        workspace_id=workspace_id,
        name=name,
        type="checking",
        balance=Decimal("0"),
        currency="USD",
    )
    session.add(account)
    await session.flush()
    return account


async def _tx(
    session: AsyncSession,
    user_id,
    workspace_id,
    account_id,
    amount: str,
    *,
    type_: str = "debit",
    when: date | None = None,
    description: str = "Monthly transfer",
    source: str = "sync",
) -> Transaction:
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=user_id,
        workspace_id=workspace_id,
        account_id=account_id,
        description=description,
        amount=Decimal(amount),
        currency="USD",
        amount_primary=Decimal(amount),
        date=when or date.today(),
        type=type_,
        source=source,
        created_at=datetime.now(timezone.utc),
    )
    session.add(tx)
    await session.flush()
    return tx


async def _household(session: AsyncSession, test_user, test_workspace):
    """Owner and partner, one workspace, one household group, an account
    each. Returns everything the tests name."""
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
        "mine": mine,
        "hers": hers,
    }


def _contribution(home, **overrides) -> dict:
    payload = {
        "from_member_id": str(home["partner"].id),
        "to_member_id": str(home["me"].id),
        "amount": "2000.00",
        "currency": "USD",
        "date": date.today().isoformat(),
    }
    payload.update(overrides)
    return payload


# ──────────────────── the receiver side, end to end ──────────────────────


@pytest.mark.asyncio
async def test_both_sides_are_set_on_create_and_returned_on_read(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    debit = await _tx(session, home["partner_user"].id, test_workspace.id, home["hers"].id, "2000.00")
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00", type_="credit"
    )
    await session.commit()

    created = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=auth_headers,
        json=_contribution(
            home,
            transaction_id=str(debit.id),
            receiver_transaction_id=str(credit.id),
        ),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["transaction_id"] == str(debit.id)
    assert body["receiver_transaction_id"] == str(credit.id)
    assert body["links"] == {
        "payer_transaction_id": str(debit.id),
        "receiver_transaction_id": str(credit.id),
    }

    listed = await client.get(
        f"/api/groups/{home['group'].id}/settlements", headers=auth_headers
    )
    assert listed.status_code == 200
    (row,) = listed.json()
    assert row["receiver_transaction_id"] == str(credit.id)
    assert row["links"]["payer_transaction_id"] == str(debit.id)


@pytest.mark.asyncio
async def test_the_receiver_side_can_be_set_on_update(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00", type_="credit"
    )
    await session.commit()

    created = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=auth_headers,
        json=_contribution(home),
    )
    settlement_id = created.json()["id"]

    updated = await client.patch(
        f"/api/groups/{home['group'].id}/settlements/{settlement_id}",
        headers=auth_headers,
        json={"receiver_transaction_id": str(credit.id)},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["receiver_transaction_id"] == str(credit.id)
    assert updated.json()["links"]["receiver_transaction_id"] == str(credit.id)


@pytest.mark.asyncio
async def test_nothing_synthetic_is_created_unless_it_is_asked_for(
    client, auth_headers, session, test_user, test_workspace
):
    """The receiver is a real user with a checking account, which used to
    be enough for a mirror credit to appear on its own."""
    home = await _household(session, test_user, test_workspace)
    debit = await _tx(session, home["partner_user"].id, test_workspace.id, home["hers"].id, "2000.00")
    await session.commit()
    before = len((await session.execute(select(Transaction.id))).all())

    created = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=auth_headers,
        json=_contribution(home, transaction_id=str(debit.id)),
    )
    assert created.status_code == 201, created.text
    assert created.json()["receiver_transaction_id"] is None

    after = len((await session.execute(select(Transaction.id))).all())
    assert after == before


@pytest.mark.asyncio
async def test_asking_for_a_receiver_transaction_and_linking_one_is_refused(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00", type_="credit"
    )
    await session.commit()

    refused = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=auth_headers,
        json=_contribution(
            home,
            receiver_transaction_id=str(credit.id),
            create_receiver_transaction=True,
        ),
    )
    assert refused.status_code == 400
    assert "not both" in refused.json()["detail"]


# ─────────────────── one transaction, one contribution ───────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("second_column", ["transaction_id", "receiver_transaction_id"])
async def test_a_transaction_already_linked_is_refused_in_either_column(
    client, auth_headers, session, test_user, test_workspace, second_column
):
    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00", type_="credit"
    )
    await session.commit()

    first = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=auth_headers,
        json=_contribution(home, receiver_transaction_id=str(credit.id)),
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=auth_headers,
        json=_contribution(home, **{second_column: str(credit.id)}),
    )
    assert second.status_code == 400
    assert "already linked" in second.json()["detail"]

    rows = (await session.execute(select(GroupSettlement.id))).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_a_transaction_linked_on_the_payer_side_is_refused_on_the_receiver_side(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    debit = await _tx(session, home["partner_user"].id, test_workspace.id, home["hers"].id, "2000.00")
    await session.commit()

    first = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=auth_headers,
        json=_contribution(home, transaction_id=str(debit.id)),
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=auth_headers,
        json=_contribution(home, receiver_transaction_id=str(debit.id)),
    )
    assert second.status_code == 400
    assert "already linked" in second.json()["detail"]


@pytest.mark.asyncio
async def test_the_two_sides_cannot_be_one_transaction(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    debit = await _tx(session, home["partner_user"].id, test_workspace.id, home["hers"].id, "2000.00")
    await session.commit()

    refused = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=auth_headers,
        json=_contribution(
            home, transaction_id=str(debit.id), receiver_transaction_id=str(debit.id)
        ),
    )
    assert refused.status_code == 400
    assert "two transactions" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_a_unique_index_backs_the_guard(session, test_user, test_workspace):
    """The service check cannot see a concurrent writer; the index can."""
    home = await _household(session, test_user, test_workspace)
    debit = await _tx(session, home["partner_user"].id, test_workspace.id, home["hers"].id, "2000.00")
    await session.commit()

    def _row() -> GroupSettlement:
        return GroupSettlement(
            id=uuid.uuid4(),
            group_id=home["group"].id,
            workspace_id=test_workspace.id,
            from_member_id=home["partner"].id,
            to_member_id=home["me"].id,
            amount=Decimal("2000.00"),
            currency="USD",
            date=date.today(),
            transaction_id=debit.id,
        )

    session.add(_row())
    await session.commit()
    session.add(_row())
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_a_shared_transaction_cannot_be_linked_as_a_contribution(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    groceries = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "300.00", description="Groceries"
    )
    await split_service.replace_splits(
        session,
        groceries,
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

    refused = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=auth_headers,
        json=_contribution(home, transaction_id=str(groceries.id)),
    )
    assert refused.status_code == 400
    assert "shared in a group" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_a_linked_transaction_cannot_be_given_shares(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00", type_="credit"
    )
    await session.commit()

    created = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=auth_headers,
        json=_contribution(home, receiver_transaction_id=str(credit.id)),
    )
    assert created.status_code == 201, created.text

    refused = await client.patch(
        f"/api/transactions/{credit.id}",
        headers=auth_headers,
        json={
            "splits": {
                "share_type": "equal",
                "splits": [
                    {"group_member_id": str(home["me"].id)},
                    {"group_member_id": str(home["partner"].id)},
                ],
            }
        },
    )
    assert refused.status_code == 400
    assert "cannot be shared" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_bulk_sharing_skips_a_linked_transaction(
    client, auth_headers, session, test_user, test_workspace
):
    """A bulk action over a page of rows must not fail wholesale because
    one of them is already a contribution."""
    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00", type_="credit"
    )
    groceries = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "300.00", description="Groceries"
    )
    await session.commit()

    await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=auth_headers,
        json=_contribution(home, receiver_transaction_id=str(credit.id)),
    )

    resp = await client.patch(
        "/api/transactions/bulk-add-to-group",
        headers=auth_headers,
        json={
            "transaction_ids": [str(credit.id), str(groceries.id)],
            "group_id": str(home["group"].id),
            "share_type": "equal",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"updated": 1, "skipped": 1}


# ─────────────────────── marking from a transaction ──────────────────────


@pytest.mark.asyncio
async def test_marking_a_debit_makes_its_account_owner_the_payer(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    debit = await _tx(
        session,
        home["partner_user"].id,
        test_workspace.id,
        home["hers"].id,
        "2000.00",
        when=date(2026, 5, 20),
    )
    await session.commit()

    marked = await client.post(
        f"/api/groups/{home['group'].id}/settlements/from-transaction",
        headers=auth_headers,
        json={"transaction_id": str(debit.id), "member_id": str(home["me"].id)},
    )
    assert marked.status_code == 201, marked.text
    body = marked.json()
    assert body["from_member_id"] == str(home["partner"].id)
    assert body["to_member_id"] == str(home["me"].id)
    assert body["amount"] == "2000.00"
    assert body["currency"] == "USD"
    assert body["date"] == "2026-05-20"
    assert body["links"] == {
        "payer_transaction_id": str(debit.id),
        "receiver_transaction_id": None,
    }


@pytest.mark.asyncio
async def test_marking_a_credit_makes_its_account_owner_the_receiver(
    client, auth_headers, session, test_user, test_workspace
):
    """The household's real case: the partner transfers, the owner marks
    the credit that landed on his own account."""
    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session,
        test_user.id,
        test_workspace.id,
        home["mine"].id,
        "2000.00",
        type_="credit",
        when=date(2026, 5, 20),
    )
    await session.commit()

    marked = await client.post(
        f"/api/groups/{home['group'].id}/settlements/from-transaction",
        headers=auth_headers,
        json={"transaction_id": str(credit.id), "member_id": str(home["partner"].id)},
    )
    assert marked.status_code == 201, marked.text
    body = marked.json()
    assert body["from_member_id"] == str(home["partner"].id)
    assert body["to_member_id"] == str(home["me"].id)
    assert body["transaction_id"] is None
    assert body["receiver_transaction_id"] == str(credit.id)
    assert body["links"]["receiver_transaction_id"] == str(credit.id)


@pytest.mark.asyncio
async def test_marking_creates_no_transaction(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00", type_="credit"
    )
    await session.commit()
    before = len((await session.execute(select(Transaction.id))).all())

    await client.post(
        f"/api/groups/{home['group'].id}/settlements/from-transaction",
        headers=auth_headers,
        json={"transaction_id": str(credit.id), "member_id": str(home["partner"].id)},
    )

    after = len((await session.execute(select(Transaction.id))).all())
    assert after == before


@pytest.mark.asyncio
async def test_marking_refuses_a_counterparty_that_is_no_member(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    other = await group_service.create_group(
        session, test_workspace.id, test_user.id, GroupCreate(name="Trip")
    )
    outsider = await group_service.create_member(
        session, other.id, test_workspace.id, GroupMemberCreate(name="Outsider")
    )
    assert outsider is not None
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00", type_="credit"
    )
    await session.commit()

    refused = await client.post(
        f"/api/groups/{home['group'].id}/settlements/from-transaction",
        headers=auth_headers,
        json={"transaction_id": str(credit.id), "member_id": str(outsider.id)},
    )
    assert refused.status_code == 400
    assert "belong to the group" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_marking_refuses_the_member_whose_account_it_is(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00", type_="credit"
    )
    await session.commit()

    refused = await client.post(
        f"/api/groups/{home['group'].id}/settlements/from-transaction",
        headers=auth_headers,
        json={"transaction_id": str(credit.id), "member_id": str(home["me"].id)},
    )
    assert refused.status_code == 400
    assert "whose account this is" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_marking_refuses_a_transaction_that_is_already_a_contribution(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00", type_="credit"
    )
    await session.commit()

    url = f"/api/groups/{home['group'].id}/settlements/from-transaction"
    payload = {"transaction_id": str(credit.id), "member_id": str(home["partner"].id)}
    assert (await client.post(url, headers=auth_headers, json=payload)).status_code == 201
    second = await client.post(url, headers=auth_headers, json=payload)
    assert second.status_code == 400
    assert "already linked" in second.json()["detail"]


@pytest.mark.asyncio
async def test_marking_refuses_a_shared_transaction(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    groceries = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "300.00", description="Groceries"
    )
    await split_service.replace_splits(
        session,
        groceries,
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

    refused = await client.post(
        f"/api/groups/{home['group'].id}/settlements/from-transaction",
        headers=auth_headers,
        json={"transaction_id": str(groceries.id), "member_id": str(home["partner"].id)},
    )
    assert refused.status_code == 400
    assert "shared in a group" in refused.json()["detail"]


# ──────────────────────── pairing the second leg ─────────────────────────


@pytest.mark.asyncio
async def test_pairing_the_second_leg_attaches_it_to_the_existing_contribution(
    session, test_user, test_workspace
):
    """Her debit is imported after his credit was already marked. One
    bank transfer, one event, one contribution."""
    from app.services import transfer_detection_service

    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session,
        test_user.id,
        test_workspace.id,
        home["mine"].id,
        "2000.00",
        type_="credit",
        when=date(2026, 5, 20),
    )
    await session.commit()

    marked = await settlement_service.mark_transaction_as_contribution(
        session,
        home["group"].id,
        test_workspace.id,
        test_user.id,
        __import__(
            "app.schemas.group_settlement", fromlist=["MarkContributionFromTransaction"]
        ).MarkContributionFromTransaction(
            transaction_id=credit.id, member_id=home["partner"].id
        ),
    )
    assert marked is not None

    debit = await _tx(
        session,
        home["partner_user"].id,
        test_workspace.id,
        home["hers"].id,
        "2000.00",
        when=date(2026, 5, 20),
    )
    await session.commit()

    pairs = await transfer_detection_service.detect_transfer_pairs(
        session, test_workspace.id, candidate_ids=[debit.id]
    )
    await session.commit()
    assert pairs == 1

    rows = (await session.execute(select(GroupSettlement))).scalars().all()
    assert len(rows) == 1, "the second leg must not create a second contribution"
    await session.refresh(rows[0])
    assert rows[0].transaction_id == debit.id
    assert rows[0].receiver_transaction_id == credit.id


# ────────────────────────────── deleting ─────────────────────────────────


@pytest.mark.asyncio
async def test_deleting_a_contribution_never_deletes_a_bank_row(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    debit = await _tx(session, home["partner_user"].id, test_workspace.id, home["hers"].id, "2000.00")
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00", type_="credit"
    )
    await session.commit()

    created = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=auth_headers,
        json=_contribution(
            home,
            transaction_id=str(debit.id),
            receiver_transaction_id=str(credit.id),
        ),
    )
    settlement_id = created.json()["id"]

    deleted = await client.delete(
        f"/api/groups/{home['group'].id}/settlements/{settlement_id}",
        headers=auth_headers,
    )
    assert deleted.status_code == 204

    assert await session.get(Transaction, debit.id) is not None
    assert await session.get(Transaction, credit.id) is not None


@pytest.mark.asyncio
async def test_deleting_a_contribution_removes_the_transaction_it_created(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)

    created = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=auth_headers,
        json=_contribution(
            home,
            from_member_id=str(home["me"].id),
            to_member_id=str(home["partner"].id),
            account_id=str(home["mine"].id),
        ),
    )
    assert created.status_code == 201, created.text
    synthetic_id = uuid.UUID(created.json()["transaction_id"])
    assert await session.get(Transaction, synthetic_id) is not None

    deleted = await client.delete(
        f"/api/groups/{home['group'].id}/settlements/{created.json()['id']}",
        headers=auth_headers,
    )
    assert deleted.status_code == 204
    session.expire_all()
    assert await session.get(Transaction, synthetic_id) is None


# ───────────────────────── history, read anew ────────────────────────────


@pytest.mark.asyncio
async def test_a_legacy_credit_in_the_payer_column_is_read_as_the_receiver_side(
    client, auth_headers, session, test_user, test_workspace
):
    """Every one of the owner's recorded settlements links the credit
    that landed on his account in the payer-side column, because the
    receiver side did not exist when they were written. They are read on
    the side the money really moved, and the row is not touched."""
    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session,
        test_user.id,
        test_workspace.id,
        home["mine"].id,
        "2000.00",
        type_="credit",
        when=date(2026, 5, 20),
    )
    legacy = GroupSettlement(
        id=uuid.uuid4(),
        group_id=home["group"].id,
        workspace_id=test_workspace.id,
        from_member_id=home["partner"].id,
        to_member_id=home["me"].id,
        amount=Decimal("2000.00"),
        currency="USD",
        date=date(2026, 5, 20),
        transaction_id=credit.id,
    )
    session.add(legacy)
    await session.commit()
    legacy_id, credit_id = legacy.id, credit.id

    listed = await client.get(
        f"/api/groups/{home['group'].id}/settlements", headers=auth_headers
    )
    (row,) = listed.json()
    assert row["links"] == {
        "payer_transaction_id": None,
        "receiver_transaction_id": str(credit.id),
    }
    # And on the group page's own list of the period's contributions.
    period = await client.get(
        f"/api/groups/{home['group'].id}/period",
        headers=auth_headers,
        params={"start": "2026-05-01", "end": "2026-06-01"},
    )
    (contribution,) = period.json()["contributions"]
    assert contribution["links"]["receiver_transaction_id"] == str(credit.id)
    assert contribution["links"]["payer_transaction_id"] is None

    # No data change: the column still holds what it held.
    session.expire_all()
    stored = await session.get(GroupSettlement, legacy_id)
    assert stored is not None
    assert stored.transaction_id == credit_id
    assert stored.receiver_transaction_id is None


@pytest.mark.asyncio
async def test_a_legacy_debit_in_the_payer_column_stays_the_payer_side(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    debit = await _tx(session, home["partner_user"].id, test_workspace.id, home["hers"].id, "2000.00")
    await session.commit()

    created = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=auth_headers,
        json=_contribution(home, transaction_id=str(debit.id)),
    )
    assert created.json()["links"] == {
        "payer_transaction_id": str(debit.id),
        "receiver_transaction_id": None,
    }


# ───────────────────── the migration, against real books ─────────────────


def _load_migration():
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "091_contribution_link_unique.py"
    )
    spec = importlib.util.spec_from_file_location("migration_091", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _books_without_the_indexes():
    """A `group_settlements` table shaped like the one in production
    before 091 — no unique indexes — so violating rows can exist to be
    found. The test database already has the indexes, which is the whole
    point of them."""
    import sqlite3

    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE group_settlements (
            id TEXT PRIMARY KEY,
            transaction_id TEXT,
            receiver_transaction_id TEXT
        )
        """
    )
    return connection


def _insert(connection, settlement_id, payer=None, receiver=None):
    connection.execute(
        "INSERT INTO group_settlements VALUES (?, ?, ?)",
        (settlement_id, payer, receiver),
    )
    # SQLAlchemy rolls back when it takes the connection over, so the
    # rows have to be committed before the migration looks at them.
    connection.commit()


def test_the_migration_passes_over_books_that_keep_the_rule():
    import sqlalchemy as sa

    migration = _load_migration()
    connection = _books_without_the_indexes()
    _insert(connection, "s1", payer="tx-a")
    _insert(connection, "s2", receiver="tx-b")
    _insert(connection, "s3")
    _insert(connection, "s4")  # two unlinked sides do not collide

    engine = sa.create_engine("sqlite://", creator=lambda: connection, poolclass=sa.pool.StaticPool)
    with engine.connect() as bind:
        assert migration._violations(bind) == []


def test_the_migration_names_every_row_that_breaks_the_rule():
    """It stops rather than choosing which link to drop: which of two
    contributions owns a transaction is the user's call."""
    import sqlalchemy as sa

    migration = _load_migration()
    connection = _books_without_the_indexes()
    _insert(connection, "same-payer-1", payer="tx-a")
    _insert(connection, "same-payer-2", payer="tx-a")
    _insert(connection, "same-receiver-1", receiver="tx-b")
    _insert(connection, "same-receiver-2", receiver="tx-b")
    _insert(connection, "cross-payer", payer="tx-c")
    _insert(connection, "cross-receiver", receiver="tx-c")

    engine = sa.create_engine("sqlite://", creator=lambda: connection, poolclass=sa.pool.StaticPool)
    with engine.connect() as bind:
        problems = migration._violations(bind)

    joined = "\n".join(problems)
    assert "tx-a" in joined and "same-payer-1" in joined and "same-payer-2" in joined
    assert "tx-b" in joined and "same-receiver-1" in joined and "same-receiver-2" in joined
    assert "tx-c" in joined and "cross-payer" in joined and "cross-receiver" in joined
    assert len(problems) == 3


def _books_before_091():
    """The table as migrations 044 and 045 leave it: both link columns
    indexed, neither unique."""
    import sqlalchemy as sa

    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE group_settlements (
                id TEXT PRIMARY KEY,
                transaction_id TEXT,
                receiver_transaction_id TEXT
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_group_settlements_transaction_id "
            "ON group_settlements (transaction_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_group_settlements_receiver_transaction_id "
            "ON group_settlements (receiver_transaction_id)"
        )
    return engine


def _index_names(connection) -> set[str]:
    import sqlalchemy as sa

    return {i["name"] for i in sa.inspect(connection).get_indexes("group_settlements")}


def test_the_migration_swaps_the_plain_indexes_for_unique_ones_and_back(monkeypatch):
    """Upgrade and downgrade both run, and the downgrade puts back what
    was there. Exercised on SQLite; the same DDL is what Postgres gets."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = _load_migration()
    engine = _books_before_091()

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)

        assert "ix_group_settlements_transaction_id" in _index_names(connection)
        migration.upgrade()

        after_upgrade = _index_names(connection)
        assert "uq_group_settlements_transaction_id" in after_upgrade
        assert "uq_group_settlements_receiver_transaction_id" in after_upgrade
        assert "ix_group_settlements_transaction_id" not in after_upgrade

        connection.exec_driver_sql(
            "INSERT INTO group_settlements VALUES ('a', 'tx-a', NULL)"
        )
        # Two unlinked sides never collide.
        connection.exec_driver_sql("INSERT INTO group_settlements VALUES ('b', NULL, NULL)")
        connection.exec_driver_sql("INSERT INTO group_settlements VALUES ('c', NULL, NULL)")
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            connection.exec_driver_sql(
                "INSERT INTO group_settlements VALUES ('d', 'tx-a', NULL)"
            )

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)

        migration.downgrade()
        after_downgrade = _index_names(connection)
        assert after_downgrade == {
            "ix_group_settlements_transaction_id",
            "ix_group_settlements_receiver_transaction_id",
        }
        # The rule is off again, which is what a downgrade means.
        connection.exec_driver_sql(
            "INSERT INTO group_settlements VALUES ('e', 'tx-a', NULL)"
        )


def test_the_migration_stops_before_touching_books_that_break_the_rule(monkeypatch):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = _load_migration()
    engine = _books_before_091()

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO group_settlements VALUES ('one', 'tx-a', NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO group_settlements VALUES ('two', 'tx-a', NULL)"
        )
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)

        with pytest.raises(RuntimeError) as raised:
            migration.upgrade()

        message = str(raised.value)
        assert "tx-a" in message and "one" in message and "two" in message
        assert "Nothing has been changed" in message
        # And nothing was: the old indexes are still the ones in place.
        assert "ix_group_settlements_transaction_id" in _index_names(connection)


# ─────────────────── one transfer, one contribution ──────────────────────


def _mark_payload(home, transaction, member):
    return {"transaction_id": str(transaction.id), "member_id": str(member.id)}


@pytest.mark.asyncio
async def test_marking_both_legs_of_one_transfer_gives_one_contribution(
    client, auth_headers, session, test_user, test_workspace
):
    """Both accounts are imported, so the credit and the debit are both
    markable. They are one transfer and must stay one contribution: two
    would credit her 4 000 for 2 000 that moved once, and no later
    pairing could put it right."""
    home = await _household(session, test_user, test_workspace)
    when = date(2026, 5, 20)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00",
        type_="credit", when=when,
    )
    debit = await _tx(
        session, home["partner_user"].id, test_workspace.id, home["hers"].id, "2000.00",
        when=when,
    )
    await session.commit()

    url = f"/api/groups/{home['group'].id}/settlements/from-transaction"
    first = await client.post(url, headers=auth_headers, json=_mark_payload(home, credit, home["partner"]))
    assert first.status_code == 201, first.text
    second = await client.post(url, headers=auth_headers, json=_mark_payload(home, debit, home["me"]))
    assert second.status_code == 201, second.text

    assert second.json()["id"] == first.json()["id"]
    assert second.json()["links"] == {
        "payer_transaction_id": str(debit.id),
        "receiver_transaction_id": str(credit.id),
    }

    rows = (await session.execute(select(GroupSettlement))).scalars().all()
    assert len(rows) == 1

    # And the pot moved once: 2 000 in, not 4 000.
    period = await client.get(
        f"/api/groups/{home['group'].id}/period",
        headers=auth_headers,
        params={"start": "2026-05-01", "end": "2026-06-01"},
    )
    body = period.json()
    assert len(body["contributions"]) == 1
    made = {
        p["member_id"]: p["contributions_made"]
        for p in body["positions"]
        if p["currency"] == "USD"
    }
    assert Decimal(made[str(home["partner"].id)]) == Decimal("2000.00")
    assert Decimal(made[str(home["me"].id)]) == Decimal("0")


@pytest.mark.asyncio
async def test_the_second_leg_joins_even_when_the_first_was_a_debit(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    when = date(2026, 5, 20)
    debit = await _tx(
        session, home["partner_user"].id, test_workspace.id, home["hers"].id, "2000.00",
        when=when,
    )
    # One day apart, inside the window transfer detection pairs legs over.
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00",
        type_="credit", when=date(2026, 5, 21),
    )
    await session.commit()

    url = f"/api/groups/{home['group'].id}/settlements/from-transaction"
    first = await client.post(url, headers=auth_headers, json=_mark_payload(home, debit, home["me"]))
    assert first.status_code == 201, first.text
    second = await client.post(url, headers=auth_headers, json=_mark_payload(home, credit, home["partner"]))
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]

    rows = (await session.execute(select(GroupSettlement))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_two_separate_transfers_of_the_same_amount_stay_two_contributions(
    client, auth_headers, session, test_user, test_workspace
):
    """Same members, same amount, but a month apart — two transfers, and
    nothing may fold them into one."""
    home = await _household(session, test_user, test_workspace)
    may = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00",
        type_="credit", when=date(2026, 5, 20),
    )
    june = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00",
        type_="credit", when=date(2026, 6, 20),
    )
    await session.commit()

    url = f"/api/groups/{home['group'].id}/settlements/from-transaction"
    first = await client.post(url, headers=auth_headers, json=_mark_payload(home, may, home["partner"]))
    second = await client.post(url, headers=auth_headers, json=_mark_payload(home, june, home["partner"]))
    assert first.status_code == 201 and second.status_code == 201, second.text
    assert first.json()["id"] != second.json()["id"]

    rows = (await session.execute(select(GroupSettlement))).scalars().all()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_a_same_side_leg_never_joins_an_existing_contribution(
    client, auth_headers, session, test_user, test_workspace
):
    """Two credits on the same day for the same amount are two
    contributions: the side this one wants is already taken."""
    home = await _household(session, test_user, test_workspace)
    when = date(2026, 5, 20)
    one = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00",
        type_="credit", when=when, description="Transfer one",
    )
    two = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00",
        type_="credit", when=when, description="Transfer two",
    )
    await session.commit()

    url = f"/api/groups/{home['group'].id}/settlements/from-transaction"
    first = await client.post(url, headers=auth_headers, json=_mark_payload(home, one, home["partner"]))
    second = await client.post(url, headers=auth_headers, json=_mark_payload(home, two, home["partner"]))
    assert first.status_code == 201 and second.status_code == 201, second.text
    assert first.json()["id"] != second.json()["id"]


@pytest.mark.asyncio
async def test_two_possible_other_legs_are_refused_rather_than_guessed(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    when = date(2026, 5, 20)
    for description in ("Transfer one", "Transfer two"):
        session.add(
            GroupSettlement(
                id=uuid.uuid4(),
                group_id=home["group"].id,
                workspace_id=test_workspace.id,
                from_member_id=home["partner"].id,
                to_member_id=home["me"].id,
                amount=Decimal("2000.00"),
                currency="USD",
                date=when,
                notes=description,
            )
        )
    debit = await _tx(
        session, home["partner_user"].id, test_workspace.id, home["hers"].id, "2000.00",
        when=when,
    )
    await session.commit()

    refused = await client.post(
        f"/api/groups/{home['group'].id}/settlements/from-transaction",
        headers=auth_headers,
        json=_mark_payload(home, debit, home["me"]),
    )
    assert refused.status_code == 400
    assert "More than one contribution" in refused.json()["detail"]


# ───────────────── a link has to sit on the right account ────────────────


@pytest.mark.asyncio
async def test_a_credit_on_her_account_cannot_be_his_receiver_side(
    client, auth_headers, session, test_user, test_workspace
):
    """Both accounts are in one workspace, so the owner can reach her
    rows. Filing one as his side would take the wrong bank row out of
    profit and loss and leave the right one in."""
    home = await _household(session, test_user, test_workspace)
    her_credit = await _tx(
        session, home["partner_user"].id, test_workspace.id, home["hers"].id, "2000.00",
        type_="credit",
    )
    await session.commit()

    refused = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=auth_headers,
        json=_contribution(home, receiver_transaction_id=str(her_credit.id)),
    )
    assert refused.status_code == 400
    assert "Partner's account" in refused.json()["detail"]
    assert "receiver side" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_a_debit_on_his_account_cannot_be_her_payer_side(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    his_debit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00"
    )
    await session.commit()

    refused = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=auth_headers,
        json=_contribution(home, transaction_id=str(his_debit.id)),
    )
    assert refused.status_code == 400
    assert "Me's account" in refused.json()["detail"]
    assert "payer side" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_moving_the_members_of_a_linked_contribution_is_refused(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    debit = await _tx(
        session, home["partner_user"].id, test_workspace.id, home["hers"].id, "2000.00"
    )
    await session.commit()

    created = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=auth_headers,
        json=_contribution(home, transaction_id=str(debit.id)),
    )
    assert created.status_code == 201, created.text

    refused = await client.patch(
        f"/api/groups/{home['group'].id}/settlements/{created.json()['id']}",
        headers=auth_headers,
        json={
            "from_member_id": str(home["me"].id),
            "to_member_id": str(home["partner"].id),
        },
    )
    assert refused.status_code == 400
    assert "Partner's account" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_a_transaction_on_an_account_no_member_owns_is_still_linkable(
    client, auth_headers, session, test_user, test_workspace
):
    """An outside or cash account has no member to contradict, which is
    the behaviour that was there before and stays."""
    home = await _household(session, test_user, test_workspace)
    outsider = await _partner(session, test_workspace)
    theirs = await _account(session, outsider.id, test_workspace.id, "Theirs")
    debit = await _tx(session, outsider.id, test_workspace.id, theirs.id, "2000.00")
    await session.commit()

    created = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=auth_headers,
        json=_contribution(home, transaction_id=str(debit.id)),
    )
    assert created.status_code == 201, created.text


# ──────────── either side of a contribution is your own business ─────────


async def _headers_for(client, user: User, workspace) -> dict:
    token = (
        await client.post(
            "/api/auth/login",
            data={"username": user.email, "password": PARTNER_PASSWORD},
        )
    ).json()["access_token"]
    return {
        "Authorization": f"Bearer {token}",
        "X-Workspace-Id": str(workspace.id),
    }


@pytest.mark.asyncio
async def test_a_linked_member_can_mark_a_credit_she_received(
    client, auth_headers, session, test_user, test_workspace
):
    """Being paid is as much her own first-hand knowledge as paying is,
    and in this household she is the one who sees her own books."""
    home = await _household(session, test_user, test_workspace)
    her_credit = await _tx(
        session, home["partner_user"].id, test_workspace.id, home["hers"].id, "500.00",
        type_="credit",
    )
    await session.commit()
    hers = await _headers_for(client, home["partner_user"], test_workspace)

    marked = await client.post(
        f"/api/groups/{home['group'].id}/settlements/from-transaction",
        headers=hers,
        json={"transaction_id": str(her_credit.id), "member_id": str(home["me"].id)},
    )
    assert marked.status_code == 201, marked.text
    body = marked.json()
    assert body["from_member_id"] == str(home["me"].id)
    assert body["to_member_id"] == str(home["partner"].id)
    assert body["receiver_transaction_id"] == str(her_credit.id)


@pytest.mark.asyncio
async def test_a_linked_member_can_record_a_contribution_made_to_her(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    hers = await _headers_for(client, home["partner_user"], test_workspace)

    created = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=hers,
        json=_contribution(
            home,
            from_member_id=str(home["me"].id),
            to_member_id=str(home["partner"].id),
        ),
    )
    assert created.status_code == 201, created.text


@pytest.mark.asyncio
async def test_a_linked_member_cannot_record_between_two_other_people(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    third = await group_service.create_member(
        session, home["group"].id, test_workspace.id, GroupMemberCreate(name="Third")
    )
    assert third is not None
    await session.commit()
    hers = await _headers_for(client, home["partner_user"], test_workspace)

    refused = await client.post(
        f"/api/groups/{home['group'].id}/settlements",
        headers=hers,
        json=_contribution(
            home,
            from_member_id=str(home["me"].id),
            to_member_id=str(third.id),
        ),
    )
    assert refused.status_code == 403
    assert "you are part of" in refused.json()["detail"]


# ─────────── history, when the second leg arrives after the fact ─────────


def _legacy_row(home, workspace, credit, *, when=None, notes=None) -> GroupSettlement:
    """A settlement as the owner's books hold them: the credit that
    landed on his account, stored in the payer-side column, because the
    receiver side did not exist when it was written."""
    return GroupSettlement(
        id=uuid.uuid4(),
        group_id=home["group"].id,
        workspace_id=workspace.id,
        from_member_id=home["partner"].id,
        to_member_id=home["me"].id,
        amount=Decimal("2000.00"),
        currency="USD",
        date=when or date(2026, 5, 20),
        transaction_id=credit.id,
        notes=notes,
    )


@pytest.mark.asyncio
async def test_marking_the_debit_of_a_legacy_contribution_joins_it(
    client, auth_headers, session, test_user, test_workspace
):
    """The matcher has to read the links, not the columns: a legacy row's
    credit occupies the receiver side, so its payer side is free and her
    debit belongs there. Reading the raw column instead would find no
    candidate and write a second 2 000 nothing could repair."""
    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00",
        type_="credit", when=date(2026, 5, 20),
    )
    legacy = _legacy_row(home, test_workspace, credit)
    session.add(legacy)
    debit = await _tx(
        session, home["partner_user"].id, test_workspace.id, home["hers"].id, "2000.00",
        when=date(2026, 5, 20),
    )
    await session.commit()
    legacy_id, credit_id, debit_id = legacy.id, credit.id, debit.id

    marked = await client.post(
        f"/api/groups/{home['group'].id}/settlements/from-transaction",
        headers=auth_headers,
        json={"transaction_id": str(debit.id), "member_id": str(home["me"].id)},
    )
    assert marked.status_code == 201, marked.text
    assert marked.json()["id"] == str(legacy_id)

    rows = (await session.execute(select(GroupSettlement))).scalars().all()
    assert len(rows) == 1

    # The columns are normalised in passing: the credit moves to the side
    # it was always read on, because the payer's leg needs its place.
    session.expire_all()
    stored = await session.get(GroupSettlement, legacy_id)
    assert stored is not None
    assert stored.transaction_id == debit_id
    assert stored.receiver_transaction_id == credit_id


@pytest.mark.asyncio
async def test_a_legacy_contribution_takes_a_notes_only_edit(
    client, auth_headers, session, test_user, test_workspace
):
    """Every contribution in the owner's books is a legacy row. Judging
    its credit on the payer side would refuse him a note."""
    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00", type_="credit"
    )
    legacy = _legacy_row(home, test_workspace, credit)
    session.add(legacy)
    await session.commit()

    url = f"/api/groups/{home['group'].id}/settlements/{legacy.id}"
    noted = await client.patch(url, headers=auth_headers, json={"notes": "May"})
    assert noted.status_code == 200, noted.text
    assert noted.json()["notes"] == "May"

    amended = await client.patch(url, headers=auth_headers, json={"amount": "2100.00"})
    assert amended.status_code == 200, amended.text
    assert amended.json()["amount"] == "2100.00"

    # Its links still read as they did, and the row was not rewritten.
    assert amended.json()["links"] == {
        "payer_transaction_id": None,
        "receiver_transaction_id": str(credit.id),
    }


@pytest.mark.asyncio
async def test_turning_a_legacy_contribution_round_is_still_refused(
    client, auth_headers, session, test_user, test_workspace
):
    """A member change is exactly the edit that can put a link on the
    wrong side, so that one is checked."""
    home = await _household(session, test_user, test_workspace)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00", type_="credit"
    )
    legacy = _legacy_row(home, test_workspace, credit)
    session.add(legacy)
    await session.commit()

    refused = await client.patch(
        f"/api/groups/{home['group'].id}/settlements/{legacy.id}",
        headers=auth_headers,
        json={
            "from_member_id": str(home["me"].id),
            "to_member_id": str(home["partner"].id),
        },
    )
    assert refused.status_code == 400
    assert "Me's account" in refused.json()["detail"]
    assert "receiver side" in refused.json()["detail"]


# ──────────── two transfers of one amount, legs already paired ───────────


@pytest.mark.asyncio
async def test_a_leg_never_joins_the_contribution_of_another_transfer(
    client, auth_headers, session, test_user, test_workspace
):
    """Two 2 000 transfers on the same day, both already paired by the
    bank. Marking one credit and then the *other* transfer's debit must
    not cross them over: the matcher follows the pairing."""
    home = await _household(session, test_user, test_workspace)
    when = date(2026, 5, 20)
    pairs = []
    for _ in range(2):
        pair_id = uuid.uuid4()
        credit = await _tx(
            session, test_user.id, test_workspace.id, home["mine"].id, "2000.00",
            type_="credit", when=when,
        )
        debit = await _tx(
            session, home["partner_user"].id, test_workspace.id, home["hers"].id,
            "2000.00", when=when,
        )
        credit.transfer_pair_id = pair_id
        debit.transfer_pair_id = pair_id
        pairs.append((credit, debit))
    await session.commit()

    (credit_one, _debit_one), (_credit_two, debit_two) = pairs
    url = f"/api/groups/{home['group'].id}/settlements/from-transaction"

    first = await client.post(
        url, headers=auth_headers,
        json={"transaction_id": str(credit_one.id), "member_id": str(home["partner"].id)},
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        url, headers=auth_headers,
        json={"transaction_id": str(debit_two.id), "member_id": str(home["me"].id)},
    )
    assert second.status_code == 201, second.text
    assert second.json()["id"] != first.json()["id"], (
        "the second transfer's debit joined the first transfer's contribution"
    )

    rows = (await session.execute(select(GroupSettlement))).scalars().all()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_a_pair_partner_that_is_a_contribution_is_the_only_candidate(
    client, auth_headers, session, test_user, test_workspace
):
    home = await _household(session, test_user, test_workspace)
    when = date(2026, 5, 20)
    pair_id = uuid.uuid4()
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00",
        type_="credit", when=when,
    )
    debit = await _tx(
        session, home["partner_user"].id, test_workspace.id, home["hers"].id, "2000.00",
        when=when,
    )
    credit.transfer_pair_id = pair_id
    debit.transfer_pair_id = pair_id
    await session.commit()

    url = f"/api/groups/{home['group'].id}/settlements/from-transaction"
    first = await client.post(
        url, headers=auth_headers,
        json={"transaction_id": str(credit.id), "member_id": str(home["partner"].id)},
    )
    second = await client.post(
        url, headers=auth_headers,
        json={"transaction_id": str(debit.id), "member_id": str(home["me"].id)},
    )
    assert first.status_code == 201 and second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]


# ───────────── transfer detection obeys the same rules as a person ───────


@pytest.mark.asyncio
async def test_pairing_leaves_a_contribution_alone_when_the_other_leg_is_shared(
    session, test_user, test_workspace
):
    """His credit is a contribution and her matching debit carries 50/50
    shares. Linking it would make the same money both the pot's spending
    and a payment into the pot, and positions would count both."""
    from app.services import transfer_detection_service

    home = await _household(session, test_user, test_workspace)
    when = date(2026, 5, 20)
    credit = await _tx(
        session, test_user.id, test_workspace.id, home["mine"].id, "2000.00",
        type_="credit", when=when,
    )
    session.add(_legacy_row(home, test_workspace, credit, when=when))
    debit = await _tx(
        session, home["partner_user"].id, test_workspace.id, home["hers"].id, "2000.00",
        when=when,
    )
    await split_service.replace_splits(
        session,
        debit,
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

    # The pairing itself still happens; only the contribution is spared.
    pairs = await transfer_detection_service.detect_transfer_pairs(
        session, test_workspace.id, candidate_ids=[debit.id]
    )
    await session.commit()
    assert pairs == 1

    row = (await session.execute(select(GroupSettlement))).scalars().one()
    await session.refresh(row)
    assert row.transaction_id == credit.id
    assert row.receiver_transaction_id is None


@pytest.mark.asyncio
async def test_pairing_leaves_a_contribution_alone_when_the_leg_is_on_the_wrong_account(
    session, test_user, test_workspace
):
    """An equal credit on her *own* second account is not the receiving
    side of a her → him contribution, however well the amounts match."""
    from app.services import transfer_detection_service

    home = await _household(session, test_user, test_workspace)
    when = date(2026, 5, 20)
    her_savings = await _account(
        session, home["partner_user"].id, test_workspace.id, "Her savings"
    )
    her_debit = await _tx(
        session, home["partner_user"].id, test_workspace.id, home["hers"].id, "2000.00",
        when=when,
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
            date=when,
            transaction_id=her_debit.id,
        )
    )
    her_credit = await _tx(
        session, home["partner_user"].id, test_workspace.id, her_savings.id, "2000.00",
        type_="credit", when=when,
    )
    await session.commit()

    await transfer_detection_service.detect_transfer_pairs(
        session, test_workspace.id, candidate_ids=[her_credit.id]
    )
    await session.commit()

    row = (await session.execute(select(GroupSettlement))).scalars().one()
    await session.refresh(row)
    assert row.transaction_id == her_debit.id
    assert row.receiver_transaction_id is None
