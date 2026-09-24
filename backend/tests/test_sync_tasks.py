import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.bank_connection import BankConnection
from app.providers.base import ProviderRateLimited
from app.tasks.sync_tasks import (
    _should_trigger_provider_refresh,
    _sync_all,
    _sync_one,
    sync_single_connection,
)
from tests.conftest import TestSessionLocal


def test_should_trigger_provider_refresh_daily():
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)

    assert _should_trigger_provider_refresh(None, now)
    assert _should_trigger_provider_refresh({"last_provider_refresh_at": "invalid"}, now)
    assert _should_trigger_provider_refresh(
        {"last_provider_refresh_at": (now - timedelta(hours=20)).isoformat()}, now
    )
    assert not _should_trigger_provider_refresh(
        {"last_provider_refresh_at": (now - timedelta(hours=4)).isoformat()}, now
    )


@pytest.mark.asyncio
async def test_sync_one_forwards_provider_refresh(session, test_user, test_workspace):
    conn = BankConnection(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        provider="test",
        external_id="provider-conn",
        institution_name="Test Bank",
        credentials={"token": "fake"},
        status="active",
        last_sync_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    session.add(conn)
    await session.commit()

    def session_maker():
        return session

    with patch(
        "app.tasks.sync_tasks.connection_service.sync_connection",
        new_callable=AsyncMock,
    ) as sync_connection:
        await _sync_one(
            session_maker,
            conn.id,
            test_user.id,
            trigger_provider_refresh=True,
        )

    sync_connection.assert_awaited_once_with(
        session,
        conn.id,
        test_workspace.id,
        test_user.id,
        trigger_provider_refresh=True,
    )


async def _stale_connection(session, user, workspace, settings=None) -> BankConnection:
    conn = BankConnection(
        id=uuid.uuid4(),
        user_id=user.id,
        workspace_id=workspace.id,
        provider="test",
        external_id=f"ext-{uuid.uuid4().hex[:8]}",
        institution_name="Test Bank",
        credentials={"token": "fake"},
        status="active",
        settings=settings,
        last_sync_at=datetime.now(timezone.utc) - timedelta(hours=5),
        created_at=datetime.now(timezone.utc),
    )
    session.add(conn)
    await session.commit()
    return conn


@pytest.mark.asyncio
async def test_sync_all_backs_off_rate_limited_connections(
    session, test_user, test_workspace
):
    """A connection still inside its rate-limit backoff is not touched, one
    whose backoff has passed is tried again, and a run the bank throttles is
    counted as rate-limited rather than synced."""
    now = datetime.now(timezone.utc)
    healthy = await _stale_connection(session, test_user, test_workspace)
    cooling = await _stale_connection(
        session, test_user, test_workspace,
        settings={"rate_limited_until": (now + timedelta(hours=2)).isoformat()},
    )
    recovered = await _stale_connection(
        session, test_user, test_workspace,
        settings={"rate_limited_until": (now - timedelta(minutes=1)).isoformat()},
    )

    async def fake_sync(_session, connection_id, *_args, **_kwargs):
        if connection_id == recovered.id:
            raise ProviderRateLimited("429: ASPSP_RATE_LIMIT_EXCEEDED")
        return None, 0

    engine = MagicMock()
    engine.dispose = AsyncMock()
    with patch(
        "app.tasks.sync_tasks._make_session_maker",
        return_value=(engine, TestSessionLocal),
    ), patch(
        "app.tasks.sync_tasks.connection_service.sync_connection",
        new_callable=AsyncMock,
        side_effect=fake_sync,
    ) as sync_connection:
        result = await _sync_all()

    tried = {call.args[1] for call in sync_connection.await_args_list}
    assert tried == {healthy.id, recovered.id}
    assert cooling.id not in tried
    assert result == {"synced": 1, "rate_limited": 1, "deferred": 1}


def test_sync_single_connection_reports_rate_limit():
    with patch(
        "app.tasks.sync_tasks._sync_one_celery",
        new_callable=AsyncMock,
        side_effect=ProviderRateLimited("429"),
    ):
        result = sync_single_connection("conn-id", "user-id")

    assert result == {"status": "rate_limited", "connection_id": "conn-id"}
