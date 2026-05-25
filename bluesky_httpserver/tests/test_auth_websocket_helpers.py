from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from bluesky_httpserver import _authentication as auth


class _FakeWebSocket:
    def __init__(self, *, headers=None, query_params=None, app=None, first_message=None, receive_error=None):
        self.headers = headers or {}
        self.query_params = query_params or {}
        self.app = app
        self.state = SimpleNamespace()
        self._first_message = first_message
        self._receive_error = receive_error
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def receive_json(self):
        if self._receive_error is not None:
            raise self._receive_error
        return self._first_message


def _make_app(*, allow_anonymous_access):
    settings = SimpleNamespace(allow_anonymous_access=allow_anonymous_access)
    return SimpleNamespace(
        dependency_overrides={
            auth.get_settings: lambda: settings,
            auth.get_authenticators: lambda: {},
            auth.get_api_access_manager: lambda: object(),
        }
    )


@pytest.mark.asyncio
async def test_websocket_first_message_auth_uses_api_key(monkeypatch):
    app = _make_app(allow_anonymous_access=False)
    websocket = _FakeWebSocket(app=app, first_message={"type": "auth", "api_key": "secret-key"})

    expected_principal = object()

    def fake_get_current_principal(**kwargs):
        assert kwargs["access_token"] is None
        assert kwargs["api_key"] == "secret-key"
        return expected_principal

    monkeypatch.setattr(auth, "get_current_principal", fake_get_current_principal)

    principal = await auth.get_current_principal_websocket(websocket=websocket, scopes=["read:monitor"])

    assert principal is expected_principal
    assert websocket.accepted is True
    assert websocket.state.already_accepted is True


@pytest.mark.asyncio
async def test_websocket_first_message_non_auth_is_rejected(monkeypatch):
    app = _make_app(allow_anonymous_access=False)
    websocket = _FakeWebSocket(app=app, first_message={"type": "subscribe", "path": "foo"})

    def fake_get_current_principal(**kwargs):
        assert kwargs["access_token"] is None
        assert kwargs["api_key"] is None
        raise HTTPException(status_code=401, detail="Invalid credentials")

    monkeypatch.setattr(auth, "get_current_principal", fake_get_current_principal)

    principal = await auth.get_current_principal_websocket(websocket=websocket, scopes=["read:monitor"])

    assert principal is None
    assert websocket.accepted is True
    assert websocket.state.already_accepted is True


@pytest.mark.asyncio
async def test_websocket_query_param_access_token_forwarded(monkeypatch):
    app = _make_app(allow_anonymous_access=True)
    websocket = _FakeWebSocket(app=app, query_params={"access_token": "query-token"})

    def fake_get_current_principal(**kwargs):
        return kwargs["access_token"]

    monkeypatch.setattr(auth, "get_current_principal", fake_get_current_principal)

    principal = await auth.get_current_principal_websocket(websocket=websocket, scopes=["read:monitor"])

    assert principal == "query-token"
    assert websocket.accepted is False
    assert websocket.state.already_accepted is False


@pytest.mark.asyncio
async def test_websocket_header_token_takes_precedence_over_query(monkeypatch):
    app = _make_app(allow_anonymous_access=True)
    websocket = _FakeWebSocket(
        app=app,
        headers={"Authorization": "Bearer header-token"},
        query_params={"access_token": "query-token"},
    )

    def fake_get_current_principal(**kwargs):
        return kwargs["access_token"]

    monkeypatch.setattr(auth, "get_current_principal", fake_get_current_principal)

    principal = await auth.get_current_principal_websocket(websocket=websocket, scopes=["read:monitor"])

    assert principal == "header-token"


@pytest.mark.asyncio
async def test_websocket_first_message_receive_error_returns_none(monkeypatch):
    app = _make_app(allow_anonymous_access=False)
    websocket = _FakeWebSocket(app=app, receive_error=TimeoutError("timed out"))

    def fake_get_current_principal(**kwargs):
        raise AssertionError("get_current_principal should not be called")

    monkeypatch.setattr(auth, "get_current_principal", fake_get_current_principal)

    principal = await auth.get_current_principal_websocket(websocket=websocket, scopes=["read:monitor"])

    assert principal is None
    assert websocket.accepted is True
    assert websocket.state.already_accepted is True
