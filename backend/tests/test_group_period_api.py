"""The period endpoint's contract, the kept balances contract, and the
MCP positions tool giving the endpoint's figures."""

import pytest
from httpx import AsyncClient

import mcp_server.tools.groups as groups_tool
from mcp_server.auth import CallContext


async def _seed(client: AsyncClient, auth_headers) -> tuple[dict, dict, dict]:
    account = (
        await client.post(
            "/api/accounts",
            headers=auth_headers,
            json={"name": "Wallet", "type": "checking", "balance": 0, "currency": "USD"},
        )
    ).json()
    group = (
        await client.post(
            "/api/groups",
            headers=auth_headers,
            json={"name": "Home", "kind": "household", "default_currency": "USD"},
        )
    ).json()
    assert group["kind"] == "household"
    members = []
    for name in ("Me", "Partner"):
        resp = await client.post(
            f"/api/groups/{group['id']}/members",
            headers=auth_headers,
            json={"name": name, "is_self": name == "Me"},
        )
        assert resp.status_code == 201, resp.text
        members.append(resp.json())
    me, partner = members

    for description, amount, type_, when in (
        ("April groceries", 100, "debit", "2026-04-30"),
        ("May groceries", 300, "debit", "2026-05-01"),
        ("May rent in", 80, "credit", "2026-05-10"),
    ):
        resp = await client.post(
            "/api/transactions",
            headers=auth_headers,
            json={
                "account_id": account["id"],
                "description": description,
                "amount": amount,
                "date": when,
                "type": type_,
                "currency": "USD",
                "splits": {
                    "share_type": "equal",
                    "splits": [{"group_member_id": m["id"]} for m in members],
                },
            },
        )
        assert resp.status_code == 201, resp.text

    resp = await client.post(
        f"/api/groups/{group['id']}/settlements",
        headers=auth_headers,
        json={
            "from_member_id": partner["id"],
            "to_member_id": me["id"],
            "amount": "60.00",
            "currency": "USD",
            "date": "2026-05-20",
        },
    )
    assert resp.status_code == 201, resp.text
    return group, me, partner


@pytest.mark.asyncio
async def test_period_endpoint_contract(client, auth_headers, test_user):
    group, me, partner = await _seed(client, auth_headers)

    resp = await client.get(
        f"/api/groups/{group['id']}/period",
        headers=auth_headers,
        params={"start": "2026-05-01", "end": "2026-06-01", "page_size": 1},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["kind"] == "household"
    assert body["start"] == "2026-05-01" and body["end"] == "2026-06-01"
    assert body["owner_member_id"] == me["id"]
    assert [(m["name"], m["is_owner_member"]) for m in body["members"]] == [
        ("Me", True),
        ("Partner", False),
    ]

    hers = next(p for p in body["positions"] if p["member_id"] == partner["id"])
    assert hers["currency"] == "USD"
    assert float(hers["share"]) == 110.0  # 150 - 40
    assert float(hers["paid"]) == 0.0
    assert float(hers["contributions_made"]) == 60.0
    assert float(hers["contributions_received"]) == 0.0
    assert float(hers["period_position"]) == 50.0
    assert float(hers["backlog"]) == 50.0
    assert float(hers["running_position"]) == 100.0
    assert float(hers["running_position_in_default_currency"]) == 100.0
    assert sum(float(p["period_position"]) for p in body["positions"]) == 0.0

    assert [float(t["amount"]) for t in body["transfers_period"]] == [50.0]
    assert [float(t["amount"]) for t in body["transfers_running"]] == [100.0]
    assert body["transfers_running"][0]["from_member_id"] == partner["id"]
    assert body["transfers_running"][0]["to_member_id"] == me["id"]

    assert [float(c["total"]) for c in body["costs"]] == [300.0]
    assert [float(c["total"]) for c in body["shared_income"]] == [80.0]
    assert [float(c["amount"]) for c in body["contributions"]] == [60.0]
    assert "receiver_transaction_id" in body["contributions"][0]
    assert body["payer_assumed_transactions"] == []

    page = body["transactions"]
    assert (page["total"], page["page"], page["page_size"]) == (2, 1, 1)
    assert [t["description"] for t in page["items"]] == ["May rent in"]
    assert float(page["items"][0]["shared_total"]) == -80.0
    assert page["items"][0]["payer_member_id"] == me["id"]


@pytest.mark.asyncio
async def test_period_endpoint_defaults_to_all_time(client, auth_headers, test_user):
    group, _, partner = await _seed(client, auth_headers)
    resp = await client.get(f"/api/groups/{group['id']}/period", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["start"] is None and body["end"] is None
    assert body["transactions"]["total"] == 3
    hers = next(p for p in body["positions"] if p["member_id"] == partner["id"])
    assert float(hers["backlog"]) == 0.0
    assert float(hers["period_position"]) == float(hers["running_position"]) == 100.0


@pytest.mark.asyncio
async def test_period_endpoint_errors(client, auth_headers, test_user):
    group, *_ = await _seed(client, auth_headers)
    url = f"/api/groups/{group['id']}/period"
    resp = await client.get(
        url, headers=auth_headers, params={"start": "2026-06-01", "end": "2026-05-01"}
    )
    assert resp.status_code == 400
    resp = await client.get(url, headers=auth_headers, params={"start": "not-a-date"})
    assert resp.status_code == 422
    resp = await client.get(
        "/api/groups/00000000-0000-0000-0000-000000000000/period", headers=auth_headers
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_balances_contract_is_unchanged_and_dates_are_optional(
    client, auth_headers, test_user
):
    group, me, partner = await _seed(client, auth_headers)
    url = f"/api/groups/{group['id']}/balances"

    resp = await client.get(url, headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"group_id", "self_member_id", "default_currency", "lines"}
    assert body["self_member_id"] == me["id"]
    assert len(body["lines"]) == 1
    line = body["lines"][0]
    assert set(line) == {"member_id", "currency", "amount", "amount_in_default_currency"}
    assert line["member_id"] == partner["id"]
    assert float(line["amount"]) == 100.0

    resp = await client.get(
        url, headers=auth_headers, params={"start": "2026-05-01", "end": "2026-06-01"}
    )
    assert resp.status_code == 200
    assert [float(ln["amount"]) for ln in resp.json()["lines"]] == [50.0]

    resp = await client.get(url, headers=auth_headers, params={"end": "2026-05-01"})
    assert [float(ln["amount"]) for ln in resp.json()["lines"]] == [50.0]

    resp = await client.get(
        url, headers=auth_headers, params={"start": "2026-06-01", "end": "2026-05-01"}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_mcp_positions_tool_gives_the_endpoints_figures(
    client, auth_headers, test_user, session
):
    group, _, partner = await _seed(client, auth_headers)
    figures = (
        "paid",
        "share",
        "contributions_made",
        "contributions_received",
        "period_position",
        "backlog",
        "running_position",
        "period_position_in_default_currency",
        "running_position_in_default_currency",
    )

    for params in ({"start": "2026-05-01", "end": "2026-06-01"}, {}):
        endpoint = (
            await client.get(
                f"/api/groups/{group['id']}/period", headers=auth_headers, params=params
            )
        ).json()
        tool = await groups_tool.get_group_positions(
            session=session,
            ctx=CallContext(user_id=test_user.id),
            group_id=group["id"],
            **params,
        )
        assert tool["start"] == endpoint["start"] and tool["end"] == endpoint["end"]
        assert tool["owner_member_id"] == endpoint["owner_member_id"]
        assert len(tool["positions"]) == len(endpoint["positions"]) == 2
        for got, want in zip(tool["positions"], endpoint["positions"]):
            assert (got["member_id"], got["currency"]) == (want["member_id"], want["currency"])
            for key in figures:
                assert got[key] == float(want[key]), key
        for key in ("transfers_period", "transfers_running"):
            assert [
                (t["from_member_id"], t["to_member_id"], t["currency"], t["amount"])
                for t in tool[key]
            ] == [
                (t["from_member_id"], t["to_member_id"], t["currency"], float(t["amount"]))
                for t in endpoint[key]
            ]
        hers = next(p for p in tool["positions"] if p["member_id"] == partner["id"])
        assert hers["member_name"] == "Partner"


@pytest.mark.asyncio
async def test_mcp_positions_tool_errors(session, test_user):
    ctx = CallContext(user_id=test_user.id)
    missing = await groups_tool.get_group_positions(
        session=session, ctx=ctx, group_id="00000000-0000-0000-0000-000000000000"
    )
    assert "error" in missing
    empty = await groups_tool.get_group_positions(session=session, ctx=ctx, group_id="")
    assert "error" in empty
    backwards = await groups_tool.get_group_positions(
        session=session,
        ctx=ctx,
        group_id="00000000-0000-0000-0000-000000000000",
        start="2026-06-01",
        end="2026-05-01",
    )
    assert "error" in backwards
