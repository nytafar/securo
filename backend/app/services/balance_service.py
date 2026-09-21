"""The kept balances contract, derived from positions.

A line still means what a member owes the group's owner: positive when
the member owes, negative when the owner owes them. It is that member's
position from `position_service`, so there is one calculation behind
both. The owner's member has no line, and neither has a member at zero.

For debits the owner paid, a line is exactly what the owner-centred
ledger gave. Two things differ on purpose: a shared credit lowers what
the others carry, and a settlement between two non-owners moves both
their lines.

With no dates a line is the running position. With `start` and/or `end`
it is the position over the half-open range [start, end).
"""

import uuid
from datetime import date
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import position_service


async def compute_balances(
    session: AsyncSession,
    group_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> Optional[dict]:
    # Visible to anyone with read access to the group — workspace members
    # plus cross-workspace linked members. They all see the same
    # who-owes-whom view.
    positions = await position_service.compute_positions(
        session, group_id, workspace_id, user_id, start=start, end=end
    )
    if positions is None:
        return None

    lines = [
        {
            "member_id": p.member_id,
            "currency": p.currency,
            "amount": p.period_position,
            "amount_in_default_currency": p.period_position_in_default_currency,
        }
        for p in positions.positions
        if p.member_id != positions.owner_member_id and p.period_position != 0
    ]

    return {
        "group_id": group_id,
        "self_member_id": positions.owner_member_id,
        "default_currency": positions.default_currency,
        "lines": lines,
    }
