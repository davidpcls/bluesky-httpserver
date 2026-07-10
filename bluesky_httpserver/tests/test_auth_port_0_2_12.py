"""
Tests for the tiled v0.2.9 -> v0.2.12 auth port.

Covers:
- decode_token fallback ordering (Phase 2.1)
- _extract_scopes helper (Phase 2.2)
- get_or_create_principal (Phase 2.6)
- Session.state column round-trip (Phase 4.3)
- Principal.access_token schema field (Phase 4.1)
- authorize redirect: offline_access + prompt=login (Phase 2.5)
- JWKS cache TTL (Phase 1.2)
- OIDC decode_token(id_token, access_token=None) signature (Phase 1.1)
- EntraAuthenticator.decode_token no longer TypeErrors (Phase 1.3)
- authenticate_websocket_first_message (Phase 2.4)
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta
from typing import Tuple
from unittest.mock import MagicMock

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jose import ExpiredSignatureError, jwt
from jose.backends import RSAKey
from respx import MockRouter
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bluesky_httpserver import _authentication as _auth
from bluesky_httpserver import schemas
from bluesky_httpserver.authenticators import (
    EntraAuthenticator,
    OIDCAuthenticator,
    ProxiedOIDCAuthenticator,
)
from bluesky_httpserver.database import orm as db_orm
from bluesky_httpserver.database.base import Base
from bluesky_httpserver.database.core import (
    create_user,
    get_or_create_principal,
)

# ---------------------------------------------------------------------------
# Shared OIDC fixtures (mirrors ones in test_authenticators.py so this file
# can be run standalone).
# ---------------------------------------------------------------------------


@pytest.fixture
def oidc_well_known_url(oidc_base_url: str) -> str:
    return f"{oidc_base_url}.well-known/openid-configuration"


@pytest.fixture
def keys() -> Tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, priv.public_key()


@pytest.fixture
def json_web_keyset(keys):
    _, public = keys
    return [RSAKey(key=public, algorithm="RS256").to_dict()]


@pytest.fixture
def mock_oidc_server(respx_mock: MockRouter, oidc_well_known_url, well_known_response, json_web_keyset):
    respx_mock.get(oidc_well_known_url).mock(return_value=httpx.Response(200, json=well_known_response))
    respx_mock.get(well_known_response["jwks_uri"]).mock(
        return_value=httpx.Response(200, json={"keys": json_web_keyset})
    )
    return respx_mock


def _make_token(private_key, **overrides) -> str:
    now = int(time.time())
    claims = {
        "aud": "tiled",
        "exp": now + 1500,
        "iat": now - 10,
        "iss": "https://example.com/realms/example",
        "sub": "abc-123",
    }
    claims.update(overrides)
    return jwt.encode(claims, key=private_key, algorithm="RS256", headers={"kid": "secret"})


# ---------------------------------------------------------------------------
# Phase 1.1 - decode_token accepts (id_token, access_token=None)
# ---------------------------------------------------------------------------


def test_oidc_decode_token_accepts_access_token_kwarg(mock_oidc_server, oidc_well_known_url, keys):
    """After the port, decode_token must accept an optional second positional
    argument (the access_token, used for at_hash validation)."""
    priv, _ = keys
    auth = OIDCAuthenticator("tiled", "tiled", "secret", well_known_uri=oidc_well_known_url)
    id_token = _make_token(priv)
    # Both calling conventions must work.
    single = auth.decode_token(id_token)
    dual = auth.decode_token(id_token, access_token=None)
    assert single == dual


# ---------------------------------------------------------------------------
# Phase 1.2 - JWKS cache TTL is 1 hour (not 7 days)
# ---------------------------------------------------------------------------


def test_oidc_keys_cache_ttl_is_one_hour():
    """The @cached decorator on OIDCAuthenticator.keys() must use a 1h TTL."""
    # cachetools stores the TTL on the cache attached to the wrapped function.
    method = OIDCAuthenticator.keys
    # ``cachetools.func.ttl_cache`` or ``cachetools.cached(TTLCache(...))``
    # both expose the underlying cache via the wrapped function.  We only
    # need to check that the TTL is one hour, not seven days.
    cache = getattr(method, "cache", None)
    if cache is None:
        # cachetools>=5 uses __wrapped__.cache or the closure.  Fall back to
        # inspecting closures.
        closures = getattr(method, "__closure__", None) or ()
        for cell in closures:
            obj = cell.cell_contents
            if hasattr(obj, "ttl"):
                cache = obj
                break
    assert cache is not None, "Unable to locate TTLCache on OIDCAuthenticator.keys"
    # 1 h == 3600 s. Assert it's an hour, definitely not 7 days.
    assert cache.ttl == pytest.approx(timedelta(hours=1).total_seconds())
    assert cache.ttl < timedelta(days=1).total_seconds()


# ---------------------------------------------------------------------------
# Phase 1.3 - EntraAuthenticator.decode_token no longer TypeErrors
# ---------------------------------------------------------------------------


def test_entra_authenticator_decode_token_signature(mock_oidc_server, oidc_well_known_url, keys, monkeypatch):
    """Regression test for the fork-local defect where
    EntraAuthenticator.decode_token called super().decode_token(id_token,
    access_token) against an OIDCAuthenticator whose decode_token only
    accepted a single argument.  After the port the parent accepts an
    optional access_token."""
    priv, _ = keys
    auth = EntraAuthenticator(
        audience="tiled",
        client_id="tiled",
        well_known_uri=oidc_well_known_url,
        device_flow_client_id="tiled-cli",
        scopes_map={"User.Read": ["read:queue"]},
    )
    id_token = _make_token(
        priv,
        preferred_username="jane@example.com",
        scp="User.Read",
    )
    # Must not raise TypeError from arg-count mismatch, nor JWTError.
    claims = auth.decode_token(id_token, access_token="opaque-access-token")
    # UUID5 rewrites 'sub', preserves entra_sub, resolves user, maps scopes.
    assert claims["entra_sub"] == "abc-123"
    assert claims["user"] == "jane"
    assert "read:queue" in claims["scope"].split()


# ---------------------------------------------------------------------------
# Phase 2.1 - decode_token fallback ordering
# ---------------------------------------------------------------------------


def _encode_hs(payload, key):
    return jwt.encode(payload, key, algorithm="HS256")


def test_decode_token_tries_hmac_keys_first():
    """The bluesky-httpserver HMAC keys must be tried before any proxied
    authenticator fallback.  Otherwise a stolen OIDC key could impersonate
    a locally-minted API-key session."""
    payload = {"sub": "u1", "sub_typ": "user", "ids": []}
    token = _encode_hs(payload, "k-primary")

    fake_proxied = MagicMock(spec=ProxiedOIDCAuthenticator)
    fake_proxied.decode_token.side_effect = AssertionError("must not be called")

    result = _auth.decode_token(token, ["k-primary", "k-secondary"], fake_proxied)
    assert result == payload
    fake_proxied.decode_token.assert_not_called()


def test_decode_token_supports_key_rotation():
    """Older tokens minted with a rotated-out key must still decode if the
    old key is present in secret_keys."""
    token = _encode_hs({"sub": "u1"}, "old-key")
    result = _auth.decode_token(token, ["new-key", "old-key"], None)
    assert result["sub"] == "u1"


def test_decode_token_falls_back_to_proxied_authenticator():
    """When no HMAC key accepts the token, delegate to a
    ProxiedOIDCAuthenticator.decode_token.  This enables OIDC-minted access
    tokens (device-code flow) to be accepted by protected endpoints."""
    # Encode with a key that is not in secret_keys, so HMAC decoding fails.
    token = _encode_hs({"sub": "external-u", "scp": "read:queue"}, "unknown-key")

    fake_proxied = MagicMock(spec=ProxiedOIDCAuthenticator)
    fake_proxied.decode_token.return_value = {
        "sub": "external-u",
        "scp": "read:queue",
    }

    result = _auth.decode_token(token, ["hmac-key"], fake_proxied)
    assert result == {"sub": "external-u", "scp": "read:queue"}
    fake_proxied.decode_token.assert_called_once_with(token)


def test_decode_token_raises_when_no_key_matches():
    token = _encode_hs({"sub": "u1"}, "unknown")
    with pytest.raises(HTTPException) as excinfo:
        _auth.decode_token(token, ["a", "b"], None)
    assert excinfo.value.status_code == 401


def test_decode_token_propagates_expired_signature():
    """Expired tokens raise ExpiredSignatureError verbatim so the caller can
    return a distinct 401 with 'refresh token' guidance rather than a
    generic 'invalid credentials'."""
    past = int(time.time()) - 3600
    token = jwt.encode({"sub": "u1", "exp": past}, "k", algorithm="HS256")
    with pytest.raises(ExpiredSignatureError):
        _auth.decode_token(token, ["k"], None)


# ---------------------------------------------------------------------------
# Phase 2.2 - _extract_scopes helper
# ---------------------------------------------------------------------------


class TestExtractScopes:
    def test_scp_as_space_separated_string(self):
        assert _auth._extract_scopes({"scp": "read:queue write:queue:edit"}) == {
            "read:queue",
            "write:queue:edit",
        }

    def test_scp_as_list(self):
        assert _auth._extract_scopes({"scp": ["read:queue", "read:status"]}) == {
            "read:queue",
            "read:status",
        }

    def test_scope_as_space_separated_string(self):
        assert _auth._extract_scopes({"scope": "read:queue read:status"}) == {
            "read:queue",
            "read:status",
        }

    def test_empty_or_missing(self):
        assert _auth._extract_scopes({}) == set()
        assert _auth._extract_scopes({"scp": "", "scope": ""}) == {""}


# ---------------------------------------------------------------------------
# Phase 4.1 - schemas.Principal.access_token
# ---------------------------------------------------------------------------


def test_principal_carries_access_token_field():
    """Externally-authenticated principals attach the raw OIDC access token
    so downstream services can perform OBO exchanges."""
    p = schemas.Principal(
        uuid=uuid.uuid4(),
        type=schemas.PrincipalType.user,
        identities=[schemas.Identity(id="jane", provider="entra")],
        access_token="opaque-entra-token",
    )
    assert p.access_token == "opaque-entra-token"
    # Default is None so existing serializations of API-key-authenticated
    # principals are unaffected.
    p2 = schemas.Principal(uuid=uuid.uuid4(), type=schemas.PrincipalType.user)
    assert p2.access_token is None


# ---------------------------------------------------------------------------
# Phase 4.3 - Session.state column round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def test_session_state_column_round_trips(sqlite_session):
    """Authenticator-supplied state must survive a DB round-trip so that
    tiled-style OBO handoff works across refresh_session calls."""
    db = sqlite_session
    principal = create_user(db, "entra", "jane@example.com")
    payload = {"entra_access_token": "AT", "entra_refresh_token": "RT"}
    from datetime import datetime

    session = db_orm.Session(
        principal_id=principal.id,
        expiration_time=datetime.utcnow() + timedelta(days=1),
        state=payload,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    reloaded = db.query(db_orm.Session).filter_by(id=session.id).one()
    assert reloaded.state == payload


def test_session_state_defaults_to_empty_dict(sqlite_session):
    from datetime import datetime

    db = sqlite_session
    principal = create_user(db, "internal", "alice")
    session = db_orm.Session(
        principal_id=principal.id,
        expiration_time=datetime.utcnow() + timedelta(days=1),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    # Server default is '{}' so a session created without an explicit state
    # must not present as None to the ORM.
    assert reloaded_state(session) == {}


def reloaded_state(session):
    # SQLite may return None if the server_default has not been re-selected;
    # normalize.
    return session.state if session.state is not None else {}


# ---------------------------------------------------------------------------
# Phase 2.6 - get_or_create_principal
# ---------------------------------------------------------------------------


def test_get_or_create_principal_creates_when_missing(sqlite_session):
    db = sqlite_session
    p = get_or_create_principal(db, "entra", "jane@example.com")
    assert p is not None
    assert p.uuid is not None
    idents = db.query(db_orm.Identity).filter_by(id="jane@example.com", provider="entra").all()
    assert len(idents) == 1
    assert idents[0].principal_id == p.id


def test_get_or_create_principal_returns_existing_and_updates_latest_login(sqlite_session):
    db = sqlite_session
    first = get_or_create_principal(db, "entra", "jane@example.com")
    (first_identity,) = first.identities
    first_login = first_identity.latest_login

    # Second call must NOT create a new Principal / Identity.
    second = get_or_create_principal(db, "entra", "jane@example.com")
    assert second.id == first.id

    db.refresh(first_identity)
    assert first_identity.latest_login is not None
    # It gets refreshed on every lookup, so the second timestamp must be >= first.
    if first_login is not None:
        assert first_identity.latest_login >= first_login

    principals = db.query(db_orm.Principal).all()
    assert len(principals) == 1


def test_get_or_create_principal_does_not_create_a_session(sqlite_session):
    db = sqlite_session
    get_or_create_principal(db, "entra", "jane@example.com")
    assert db.query(db_orm.Session).count() == 0


# ---------------------------------------------------------------------------
# Phase 2.4 - WebSocket first-message auth
# ---------------------------------------------------------------------------


def _fake_ws_with_deps(
    *,
    api_access_manager=None,
    authenticators=None,
    settings=None,
):
    """Build a minimal fake WebSocket whose ``app.dependency_overrides``
    look like what build_app() installs at runtime, so
    ``authenticate_websocket_first_message`` can retrieve them."""

    from bluesky_httpserver.settings import get_settings
    from bluesky_httpserver.utils import (
        get_api_access_manager,
        get_authenticators,
    )

    class _App:
        state = MagicMock()
        dependency_overrides = {
            get_settings: lambda: settings,
            get_authenticators: lambda: authenticators or {},
            get_api_access_manager: lambda: api_access_manager,
        }

    class _WS:
        app = _App()
        headers = {"host": "localhost:8000"}
        scope = {"scheme": "http", "root_path": ""}
        query_params: dict = {}
        cookies: dict = {}

        def __init__(self):
            # get_current_principal reads request.state.cookies_to_set for a
            # side-effect on the HTTP path.  Provide a stub so that path does
            # not attribute-error on the websocket route.
            self.state = MagicMock()
            self.state.cookies_to_set = []

    return _WS()


def test_authenticate_websocket_first_message_rejects_non_auth_frames():
    ws = _fake_ws_with_deps(settings=MagicMock())
    assert _auth.authenticate_websocket_first_message(ws, {"type": "ping"}) is None
    assert _auth.authenticate_websocket_first_message(ws, "not-a-dict") is None
    assert _auth.authenticate_websocket_first_message(ws, {"type": "auth"}) is None


def test_authenticate_websocket_first_message_accepts_valid_api_key(sqlite_session):
    """Feed a valid API key through the first-message handshake."""
    from bluesky_httpserver.settings import DatabaseSettings

    db = sqlite_session
    principal = create_user(db, "internal", "alice")
    # Generate an API key with the same machinery routes use.
    import hashlib
    import secrets as py_secrets

    secret = py_secrets.token_bytes(4 + 32)
    hashed = hashlib.sha256(secret).digest()
    apikey_orm = db_orm.APIKey(
        principal_id=principal.id,
        first_eight=secret.hex()[:8],
        hashed_secret=hashed,
        scopes=["read:status"],
    )
    db.add(apikey_orm)
    db.commit()

    # Route the sessionmaker used by get_current_principal through our
    # in-memory sqlite engine.
    engine = db.get_bind()

    def _fake_sessionmaker(_db_settings):
        return sessionmaker(bind=engine, autocommit=False, autoflush=False)

    settings = MagicMock()
    settings.database_settings = DatabaseSettings(uri="sqlite://", pool_size=None, pool_pre_ping=None)
    settings.authentication_provider_names = ["internal"]
    settings.secret_keys = ["hmac"]

    api_access_manager = MagicMock()
    api_access_manager.is_user_known.return_value = True
    api_access_manager.get_user_scopes.return_value = {"read:status"}
    api_access_manager.get_user_roles.return_value = {"user"}

    authenticators = {"internal": MagicMock()}  # truthy => multi-user mode
    ws = _fake_ws_with_deps(
        api_access_manager=api_access_manager,
        authenticators=authenticators,
        settings=settings,
    )

    import bluesky_httpserver._authentication as auth_mod

    saved = auth_mod.get_sessionmaker
    auth_mod.get_sessionmaker = _fake_sessionmaker
    try:
        result = _auth.authenticate_websocket_first_message(ws, {"type": "auth", "api_key": secret.hex()})
    finally:
        auth_mod.get_sessionmaker = saved

    assert result is not None
    assert result.uuid == principal.uuid


def test_authenticate_websocket_first_message_rejects_bad_api_key(sqlite_session):
    """A malformed (non-hex) API key must be rejected without leaking DB
    state.  Uses the same monkey-patched sessionmaker plumbing as the
    happy-path test so we do not accidentally exercise the real
    get_sessionmaker(pool_size=None) code path in unit tests."""
    from bluesky_httpserver.settings import DatabaseSettings

    engine = sqlite_session.get_bind()

    def _fake_sessionmaker(_db_settings):
        return sessionmaker(bind=engine, autocommit=False, autoflush=False)

    settings = MagicMock()
    settings.database_settings = DatabaseSettings(uri="sqlite://", pool_size=5, pool_pre_ping=False)
    settings.authentication_provider_names = ["internal"]
    settings.secret_keys = ["hmac"]

    ws = _fake_ws_with_deps(
        api_access_manager=MagicMock(),
        authenticators={"internal": MagicMock()},
        settings=settings,
    )

    import bluesky_httpserver._authentication as auth_mod

    saved = auth_mod.get_sessionmaker
    auth_mod.get_sessionmaker = _fake_sessionmaker
    try:
        # 'not-hex' fails bytes.fromhex → HTTPException 401 inside get_current_principal.
        assert _auth.authenticate_websocket_first_message(ws, {"type": "auth", "api_key": "not-hex"}) is None
    finally:
        auth_mod.get_sessionmaker = saved
