"""The user filter, at the API boundary.

A page may narrow the app to one person: `?user_id=` beside the
collection filter's `?account_ids=`. It resolves to the accounts that
user owns and shows their consumption — their own costs plus their
shares of shared costs, whoever paid.

Only somebody who belongs to the workspace can be filtered to. Without
that check the parameter would be a way to ask whether an arbitrary user
id exists, and an empty account set would answer with a plausible-looking
page of zeroes rather than a refusal.
"""

import uuid
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.services._query_filters import workspace_user_ids


async def assert_filterable_user(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: Optional[uuid.UUID],
) -> None:
    """Raise 404 unless `user_id` is a user of this workspace."""
    if user_id is None:
        return
    if user_id not in await workspace_user_ids(session, workspace_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User is not a member of this workspace",
        )
