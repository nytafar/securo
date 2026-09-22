"""Read-only group + member + balance + settlement exposure for the agent.

Splits are written through `propose_create_transaction` (with its
`group_id` + `splits` parameters) — there's no `propose_create_split`
tool because splits live attached to the parent transaction, not as
standalone rows.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import (
    balance_service,
    group_service,
    position_service,
    settlement_service,
)
from mcp_server.auth import CallContext
from mcp_server.registry import tool
from mcp_server.tools._helpers import num, parse_date, parse_uuid, resolve_workspace_id


@tool(
    name="list_groups",
    description=(
        "List the user's expense-sharing groups (Splitwise-style: 'Amigos', "
        "'Roommates', etc.) along with their members. Returns each group "
        "with `members: [{id, name, is_self}]` so a single call gives the "
        "model everything it needs to propose a transaction with equal/"
        "exact/percent splits. The `is_self` flag marks the member that "
        "represents the user (used to compute who-owes-who balances)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "include_archived": {"type": "boolean", "default": False},
        },
        "additionalProperties": False,
    },
    tags=["read", "groups"],
)
async def list_groups(
    *,
    session: AsyncSession,
    ctx: CallContext,
    include_archived: bool = False,
) -> dict[str, Any]:
    ws_id = await resolve_workspace_id(session, ctx)
    groups = await group_service.list_groups(session, ws_id, ctx.user_id, include_archived=include_archived)
    return {
        "items": [
            {
                "id": str(g.id),
                "name": g.name,
                "kind": g.kind,
                "default_currency": g.default_currency,
                "is_archived": bool(g.is_archived),
                "members": [
                    {
                        "id": str(m.id),
                        "name": m.name,
                        "is_self": bool(m.is_self),
                    }
                    for m in (g.members or [])
                ],
            }
            for g in groups
        ],
        "total": len(groups),
    }


@tool(
    name="get_group_balances",
    description=(
        "Compute the who-owes-who balance for one expense-sharing group. "
        "Returns lines per member: positive `amount` means the member "
        "OWES the user (self) that much in `currency`; negative means the "
        "user owes them. `amount_in_default_currency` is the value "
        "converted to the group's default currency for a single bottom "
        "line. Already accounts for past `group_settlements` (payments "
        "that closed previous balances). Use this for 'quem ainda me "
        "deve?', 'estamos quites?', 'qual o saldo do grupo Amigos?'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "format": "uuid"},
        },
        "required": ["group_id"],
        "additionalProperties": False,
    },
    tags=["read", "groups"],
)
async def get_group_balances(
    *,
    session: AsyncSession,
    ctx: CallContext,
    group_id: str,
) -> dict[str, Any]:
    gid = parse_uuid(group_id)
    if gid is None:
        return dict(error="group not found or not visible to this user")


    ws_id = await resolve_workspace_id(session, ctx)
    result = await balance_service.compute_balances(session, gid, ws_id, ctx.user_id)
    if result is None:
        return {"error": "group not found or not visible to this user"}

    # Resolve member names so the agent doesn't need a second list_groups
    # call to humanize the output.
    from sqlalchemy import select
    from app.models.group import GroupMember
    rows = (await session.execute(
        select(GroupMember).where(GroupMember.group_id == gid)
    )).scalars().all()
    name_by_id = {m.id: m.name for m in rows}
    is_self_by_id = {m.id: bool(m.is_self) for m in rows}

    lines = []
    for ln in result.get("lines", []):
        mid = ln["member_id"]
        lines.append({
            "member_id": str(mid),
            "member_name": name_by_id.get(mid),
            "is_self": is_self_by_id.get(mid, False),
            "currency": ln["currency"],
            "amount": num(ln["amount"]),
            "amount_in_default_currency": num(ln.get("amount_in_default_currency")),
        })
    return {
        "group_id": str(result["group_id"]),
        "self_member_id": str(result["self_member_id"]) if result.get("self_member_id") else None,
        "default_currency": result.get("default_currency"),
        "lines": lines,
    }


@tool(
    name="get_group_positions",
    description=(
        "Each member's position in one group's common pot for a period: "
        "`share` (what they should carry), `paid` (what they paid), "
        "`contributions_made` / `contributions_received`, the "
        "`period_position` (share - paid - made + received), the `backlog` "
        "carried in from before `start`, and the `running_position` up to "
        "`end`. Positive = the member should still move that much into the "
        "pot; negative = the pot owes them. Shared income (credits) counts "
        "negative. One row per member and currency; the same figures the "
        "group page shows, whoever asks. `transfers_period` settles the "
        "period alone and `transfers_running` includes the backlog, per "
        "currency, never converted. The range is half-open: `start` is "
        "inclusive and `end` exclusive (a calendar month is start=first "
        "day, end=first day of the next month); omit both for all time. "
        "Use for 'how much should X transfer this month?'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "format": "uuid"},
            "start": {"type": "string", "format": "date", "description": "Inclusive."},
            "end": {"type": "string", "format": "date", "description": "Exclusive."},
        },
        "required": ["group_id"],
        "additionalProperties": False,
    },
    tags=["read", "groups"],
)
async def get_group_positions(
    *,
    session: AsyncSession,
    ctx: CallContext,
    group_id: str,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    gid = parse_uuid(group_id)
    if gid is None:
        return dict(error="group not found or not visible to this user")
    start_date, end_date = parse_date(start), parse_date(end)
    if start_date and end_date and end_date < start_date:
        return {"error": "end must not be before start"}

    ws_id = await resolve_workspace_id(session, ctx)
    result = await position_service.compute_positions(
        session, gid, ws_id, ctx.user_id, start=start_date, end=end_date
    )
    if result is None:
        return {"error": "group not found or not visible to this user"}

    name_by_id = {m.id: m.name for m in result.members}

    def _transfers(rows) -> list[dict[str, Any]]:
        return [
            {
                "from_member_id": str(t.from_member_id),
                "from_member_name": name_by_id.get(t.from_member_id),
                "to_member_id": str(t.to_member_id),
                "to_member_name": name_by_id.get(t.to_member_id),
                "currency": t.currency,
                "amount": num(t.amount),
            }
            for t in rows
        ]

    return {
        "group_id": str(result.group_id),
        "kind": result.kind,
        "default_currency": result.default_currency,
        "start": result.start.isoformat() if result.start else None,
        "end": result.end.isoformat() if result.end else None,
        "owner_member_id": str(result.owner_member_id) if result.owner_member_id else None,
        "positions": [
            {
                "member_id": str(p.member_id),
                "member_name": name_by_id.get(p.member_id),
                "currency": p.currency,
                "paid": num(p.paid),
                "share": num(p.share),
                "contributions_made": num(p.contributions_made),
                "contributions_received": num(p.contributions_received),
                "period_position": num(p.period_position),
                "backlog": num(p.backlog),
                "running_position": num(p.running_position),
                "period_position_in_default_currency": num(
                    p.period_position_in_default_currency
                ),
                "running_position_in_default_currency": num(
                    p.running_position_in_default_currency
                ),
            }
            for p in result.positions
        ],
        "transfers_period": _transfers(result.transfers_period),
        "transfers_running": _transfers(result.transfers_running),
    }


@tool(
    name="list_group_settlements",
    description=(
        "List the recorded settlements — contributions, in a household "
        "group: money a member moved to carry their part of the common "
        "pot — for one group, newest first. Each row has "
        "{from_member_id, to_member_id, amount, currency, date, notes} "
        "plus both real bank transactions it is made of: "
        "`payer_transaction_id` (the account the money left) and "
        "`receiver_transaction_id` (the account it landed on), either of "
        "which may be null when that side is not imported. Use them to "
        "reconcile a contribution against the bank. Pair with "
        "`get_group_balances` when the user asks 'quem já me pagou?' or "
        "'qual o histórico de acertos?'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "group_id": {"type": "string", "format": "uuid"},
        },
        "required": ["group_id"],
        "additionalProperties": False,
    },
    tags=["read", "groups"],
)
async def list_group_settlements(
    *,
    session: AsyncSession,
    ctx: CallContext,
    group_id: str,
) -> dict[str, Any]:
    gid = parse_uuid(group_id)
    if gid is None:
        return dict(error="group not found or not visible to this user")

    ws_id = await resolve_workspace_id(session, ctx)
    rows = await settlement_service.list_settlements(session, gid, ws_id, ctx.user_id)
    if rows is None:
        return {"error": "group not found or not visible to this user"}

    # `links` carries the legacy reading: a settlement recorded before
    # the receiver side existed links the receiver's credit in the
    # payer-side column, and is reported on the side it really happened
    # on. The raw column stays visible as `transaction_id`.
    links = await settlement_service.resolve_links(
        session, [(s.transaction_id, s.receiver_transaction_id) for s in rows]
    )
    items = [
        {
            "id": str(s.id),
            "group_id": str(s.group_id),
            "from_member_id": str(s.from_member_id),
            "to_member_id": str(s.to_member_id),
            "amount": num(s.amount),
            "currency": s.currency,
            "date": s.date.isoformat() if s.date else None,
            "notes": getattr(s, "notes", None),
            "transaction_id": str(s.transaction_id) if getattr(s, "transaction_id", None) else None,
            "payer_transaction_id": (
                str(link.payer_transaction_id) if link.payer_transaction_id else None
            ),
            "receiver_transaction_id": (
                str(link.receiver_transaction_id) if link.receiver_transaction_id else None
            ),
        }
        for s, link in zip(rows, links)
    ]
    return {"items": items, "total": len(items)}
