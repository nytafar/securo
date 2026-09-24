import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bank_connection import BankConnection
from app.models.user import User
from app.core.config import get_settings
from app.providers.base import ProviderUserActionRequired, PsuContext, SessionExpiredError


@pytest.mark.asyncio
async def test_list_providers(client: AsyncClient, auth_headers):
    """Should return all known providers with their configuration status."""
    response = await client.get("/api/connections/providers", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    by_name = {p["name"]: p for p in data["providers"]}
    assert "pluggy" in by_name
    assert by_name["pluggy"]["configured"] is False
    assert by_name["pluggy"]["flow_type"] == "widget"
    assert by_name["pluggy"]["supports_asset_sync"] is True
    assert "enable_banking" in by_name
    assert by_name["enable_banking"]["flow_type"] == "oauth"
    assert by_name["enable_banking"]["requires_institution_select"] is True
    # Enable Banking (PSD2) exposes no investment holdings, so the asset-sync
    # opt-out is hidden for it; connectors that import holdings advertise it.
    assert by_name["enable_banking"]["supports_asset_sync"] is False
    assert by_name["simplefin"]["supports_asset_sync"] is True


@pytest.mark.asyncio
async def test_list_connections(
    client: AsyncClient, auth_headers, test_connection: BankConnection
):
    response = await client.get("/api/connections", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["institution_name"] == "Banco Teste"
    assert data[0]["provider"] == "test"
    assert data[0]["status"] == "active"


@pytest.mark.asyncio
async def test_list_connections_empty(client: AsyncClient, auth_headers):
    response = await client.get("/api/connections", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_oauth_url_unknown_provider(client: AsyncClient, auth_headers):
    """Should fail for unregistered provider."""
    response = await client.post(
        "/api/connections/oauth/url",
        headers=auth_headers,
        json={"provider": "nonexistent"},
    )
    assert response.status_code == 400
    assert "Unknown provider" in response.json()["detail"]


@pytest.mark.asyncio
async def test_delete_connection(
    client: AsyncClient, auth_headers, test_connection: BankConnection
):
    response = await client.delete(
        f"/api/connections/{test_connection.id}", headers=auth_headers
    )
    assert response.status_code == 204

    # Verify it's gone
    response = await client.get("/api/connections", headers=auth_headers)
    assert response.json() == []


@pytest.mark.asyncio
async def test_delete_connection_not_found(client: AsyncClient, auth_headers, test_connection):
    response = await client.delete(
        "/api/connections/00000000-0000-0000-0000-000000000000",
        headers=auth_headers,
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_connections_unauthenticated(client: AsyncClient, clean_db):
    response = await client.get("/api/connections")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_update_settings_not_found(client: AsyncClient, auth_headers):
    resp = await client.patch(
        f"/api/connections/{uuid.uuid4()}/settings",
        json={"payee_source": "merchant"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_detect_transfers(client: AsyncClient, auth_headers):
    resp = await client.post("/api/connections/transfers/detect", headers=auth_headers)
    assert resp.status_code == 200
    assert "pairs_created" in resp.json()


@pytest.mark.asyncio
async def test_unlink_transfer_not_found(client: AsyncClient, auth_headers):
    resp = await client.delete(
        f"/api/connections/transfers/{uuid.uuid4()}", headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_connect_token_success(client: AsyncClient, auth_headers):
    mock_token = MagicMock()
    mock_token.access_token = "test-token-123"
    with patch("app.services.connection_service.get_provider") as mock_gp:
        mock_gp.return_value.create_connect_token = AsyncMock(return_value=mock_token)
        resp = await client.post(
            "/api/connections/connect-token",
            json={"provider": "pluggy"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["access_token"] == "test-token-123"


@pytest.mark.asyncio
async def test_create_connect_token_value_error(client: AsyncClient, auth_headers):
    with patch("app.services.connection_service.get_provider") as mock_gp:
        mock_gp.side_effect = ValueError("Unknown provider")
        resp = await client.post(
            "/api/connections/connect-token",
            json={"provider": "invalid"},
            headers=auth_headers,
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_connect_token_server_error(client: AsyncClient, auth_headers):
    with patch("app.services.connection_service.get_provider") as mock_gp:
        mock_gp.return_value.create_connect_token = AsyncMock(
            side_effect=RuntimeError("Provider down")
        )
        resp = await client.post(
            "/api/connections/connect-token",
            json={"provider": "pluggy"},
            headers=auth_headers,
        )
        assert resp.status_code == 500


@pytest.mark.asyncio
async def test_oauth_callback_success(client: AsyncClient, auth_headers):
    conn_data = MagicMock()
    conn_data.external_id = "ext-oauth-1"
    conn_data.institution_name = "Test Bank"
    conn_data.credentials = {"token": "abc"}
    conn_data.accounts = []

    with patch("app.services.connection_service.get_provider") as mock_gp:
        mock_gp.return_value.handle_oauth_callback = AsyncMock(return_value=conn_data)
        resp = await client.post(
            "/api/connections/oauth/callback",
            json={"code": "auth-code-123", "provider": "pluggy"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["institution_name"] == "Test Bank"


@pytest.mark.asyncio
async def test_oauth_callback_token_reconnect_updates_existing(
    client: AsyncClient, auth_headers, session: AsyncSession, test_user: User
):
    conn = BankConnection(
        id=uuid.uuid4(), user_id=test_user.id, provider="simplefin",
        external_id="old-simplefin", institution_name="Old SimpleFIN",
        credentials={"access_url_enc": "old"}, status="error",
        last_sync_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    session.add(conn)
    await session.commit()

    conn_data = MagicMock()
    conn_data.external_id = "new-simplefin"
    conn_data.institution_name = "New SimpleFIN"
    conn_data.logo_url = None
    conn_data.credentials = {"access_url_enc": "new"}
    conn_data.accounts = []

    with patch("app.services.connection_service.get_provider") as mock_gp:
        mock_gp.return_value.handle_oauth_callback = AsyncMock(return_value=conn_data)
        resp = await client.post(
            "/api/connections/oauth/callback",
            json={
                "code": "fresh-simplefin-setup-token",
                "provider": "simplefin",
                "reconnect_connection_id": str(conn.id),
            },
            headers=auth_headers,
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(conn.id)
    assert body["external_id"] == "new-simplefin"
    assert body["status"] == "active"
    await session.refresh(conn)
    assert conn.credentials == {"access_url_enc": "new"}


@pytest.mark.asyncio
async def test_oauth_callback_failure(client: AsyncClient, auth_headers):
    with patch("app.services.connection_service.get_provider") as mock_gp:
        mock_gp.return_value.handle_oauth_callback = AsyncMock(
            side_effect=Exception("OAuth failed")
        )
        resp = await client.post(
            "/api/connections/oauth/callback",
            json={"code": "bad-code", "provider": "pluggy"},
            headers=auth_headers,
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_sync_connection_not_found(client: AsyncClient, auth_headers):
    resp = await client.post(
        f"/api/connections/{uuid.uuid4()}/sync", headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_sync_connection_user_action_returns_conflict(
    client: AsyncClient, auth_headers
):
    with patch("app.services.connection_service.sync_connection") as mock_sync:
        mock_sync.side_effect = ProviderUserActionRequired(
            "SimpleFIN refused the request (403)",
            code="credentials_invalid",
            help_url="https://bridge.simplefin.org/",
        )
        resp = await client.post(
            f"/api/connections/{uuid.uuid4()}/sync", headers=auth_headers,
        )

    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "message": "SimpleFIN refused the request (403)",
        "code": "credentials_invalid",
        "help_url": "https://bridge.simplefin.org/",
    }


@pytest.mark.asyncio
async def test_sync_connection_session_expired_returns_gone(
    client: AsyncClient, auth_headers
):
    with patch("app.services.connection_service.sync_connection") as mock_sync:
        mock_sync.side_effect = SessionExpiredError("SimpleFIN access URL is missing")
        resp = await client.post(
            f"/api/connections/{uuid.uuid4()}/sync", headers=auth_headers,
        )

    assert resp.status_code == 410
    assert resp.json()["detail"] == "SimpleFIN access URL is missing"


@pytest.mark.asyncio
async def test_reconnect_token_not_found(client: AsyncClient, auth_headers):
    resp = await client.post(
        f"/api/connections/{uuid.uuid4()}/reconnect-token", headers=auth_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reconnect_token_no_item_id(
    client: AsyncClient, auth_headers, session: AsyncSession, test_user: User,
):
    conn = BankConnection(
        id=uuid.uuid4(), user_id=test_user.id, provider="test",
        external_id="ext-recon-no-item", institution_name="NoItem Bank",
        credentials={}, status="active",
        last_sync_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    session.add(conn)
    await session.commit()
    resp = await client.post(
        f"/api/connections/{conn.id}/reconnect-token", headers=auth_headers,
    )
    assert resp.status_code == 400
    assert "item_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_reconnect_token_with_item_id(
    client: AsyncClient, auth_headers, session: AsyncSession, test_user: User,
):
    conn = BankConnection(
        id=uuid.uuid4(), user_id=test_user.id, provider="test",
        external_id="ext-recon-ok", institution_name="Recon Bank",
        credentials={"item_id": "item-abc-123"}, status="error",
        last_sync_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    session.add(conn)
    await session.commit()

    mock_token = MagicMock()
    mock_token.access_token = "recon-token"
    with patch("app.services.connection_service.get_provider") as mock_gp:
        mock_gp.return_value.create_connect_token = AsyncMock(return_value=mock_token)
        resp = await client.post(
            f"/api/connections/{conn.id}/reconnect-token", headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["access_token"] == "recon-token"


@pytest.mark.asyncio
async def test_update_settings_success(
    client: AsyncClient, auth_headers, session: AsyncSession, test_user: User,
):
    conn = BankConnection(
        id=uuid.uuid4(), user_id=test_user.id, provider="test",
        external_id="ext-settings-1", institution_name="Settings Bank",
        credentials={}, status="active", settings={"payee_source": "auto"},
        last_sync_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    session.add(conn)
    await session.commit()
    resp = await client.patch(
        f"/api/connections/{conn.id}/settings",
        json={"payee_source": "merchant"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["settings"]["payee_source"] == "merchant"


@pytest.mark.asyncio
async def test_manual_sync_forwards_the_present_user(
    client: AsyncClient, auth_headers, test_connection, monkeypatch
):
    """A click on Sync is attended: the client address the trusted proxy saw
    and the browser's user agent go to the provider."""
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 1)
    with patch(
        "app.services.connection_service.sync_connection", new_callable=AsyncMock
    ) as mock_sync:
        mock_sync.return_value = (test_connection, 0)
        resp = await client.post(
            f"/api/connections/{test_connection.id}/sync",
            headers={
                **auth_headers,
                "X-Forwarded-For": "198.51.100.1, 203.0.113.9",
                "User-Agent": "Mozilla/5.0 (Test)",
            },
        )

    assert resp.status_code == 200
    mock_sync.assert_awaited_once()
    psu = mock_sync.call_args.kwargs["psu"]
    assert isinstance(psu, PsuContext)
    assert psu.ip_address == "203.0.113.9"
    assert psu.user_agent == "Mozilla/5.0 (Test)"


@pytest.mark.asyncio
async def test_manual_sync_without_a_trusted_address_stays_unattended(
    client: AsyncClient, auth_headers, test_connection, monkeypatch
):
    """Behind a proxy the operator hasn't declared, there is no address to
    vouch for, so nothing is forwarded."""
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 2)
    with patch(
        "app.services.connection_service.sync_connection", new_callable=AsyncMock
    ) as mock_sync:
        mock_sync.return_value = (test_connection, 0)
        resp = await client.post(
            f"/api/connections/{test_connection.id}/sync",
            headers={**auth_headers, "X-Forwarded-For": "203.0.113.9"},
        )

    assert resp.status_code == 200
    mock_sync.assert_awaited_once()
    assert mock_sync.call_args.kwargs["psu"] is None


@pytest.mark.asyncio
async def test_manual_sync_drops_headers_that_cannot_be_forwarded(
    client: AsyncClient, auth_headers, test_connection, monkeypatch
):
    """A user agent outside printable ASCII would make the outgoing request
    fail, so it is left out rather than breaking the sync."""
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 1)
    with patch(
        "app.services.connection_service.sync_connection", new_callable=AsyncMock
    ) as mock_sync:
        mock_sync.return_value = (test_connection, 0)
        resp = await client.post(
            f"/api/connections/{test_connection.id}/sync",
            headers=[
                *((k.encode(), v.encode()) for k, v in auth_headers.items()),
                (b"X-Forwarded-For", b"203.0.113.9"),
                (b"User-Agent", "App æøå".encode()),
                (b"Accept-Language", b"nb-NO,nb;q=0.9"),
            ],
        )

    assert resp.status_code == 200
    psu = mock_sync.call_args.kwargs["psu"]
    assert psu.ip_address == "203.0.113.9"
    assert psu.user_agent is None
    assert psu.accept_language == "nb-NO,nb;q=0.9"


@pytest.mark.asyncio
async def test_oauth_callback_forwards_the_present_user(
    client: AsyncClient, auth_headers, test_connection, monkeypatch
):
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 1)
    with patch(
        "app.services.connection_service.handle_oauth_callback", new_callable=AsyncMock
    ) as mock_callback:
        mock_callback.return_value = test_connection
        resp = await client.post(
            "/api/connections/oauth/callback",
            json={"code": "auth-code", "provider": "enable_banking"},
            headers={**auth_headers, "X-Forwarded-For": "203.0.113.9"},
        )

    assert resp.status_code == 200
    mock_callback.assert_awaited_once()
    assert mock_callback.call_args.kwargs["psu"].ip_address == "203.0.113.9"
