from __future__ import annotations

from collections.abc import Iterable

from fastapi import APIRouter

from bluesky_authentication.integration import (
    AuthProviderRegistration,
    build_authentication_router,
)

from .._authentication import (
    _extract_scopes,
    base_authentication_router,
    build_auth_code_route as _build_auth_code_route,
    build_authorize_route as _build_authorize_route,
    build_device_code_authorize_route as _build_device_code_authorize_route,
    build_device_code_form_route as _build_device_code_form_route,
    build_device_code_submit_route as _build_device_code_submit_route,
    build_device_code_token_route as _build_device_code_token_route,
    build_handle_credentials_route as _build_handle_credentials_route,
    get_current_principal,
    get_current_principal_websocket,
    oauth2_scheme,
)
from .authenticator_base import (
    ExternalAuthenticator,
    InternalAuthenticator,
    UserSessionState,
)


class HttpServerAuthRouteAdapter:
    def include_base_routes(self, router: APIRouter) -> None:
        router.include_router(base_authentication_router)

    def build_internal_token_route(self, authenticator: InternalAuthenticator, provider: str):
        return _build_handle_credentials_route(authenticator, provider)

    def build_external_code_route(self, authenticator: ExternalAuthenticator, provider: str):
        return _build_auth_code_route(authenticator, provider)

    def build_external_authorize_route(
        self, authenticator: ExternalAuthenticator, provider: str
    ):
        return _build_authorize_route(authenticator, provider)

    def build_device_code_authorize_route(
        self, authenticator: ExternalAuthenticator, provider: str
    ):
        return _build_device_code_authorize_route(authenticator, provider)

    def build_device_code_form_route(
        self, authenticator: ExternalAuthenticator, provider: str
    ):
        return _build_device_code_form_route(authenticator, provider)

    def build_device_code_submit_route(
        self, authenticator: ExternalAuthenticator, provider: str
    ):
        return _build_device_code_submit_route(authenticator, provider)

    def build_device_code_token_route(
        self, authenticator: ExternalAuthenticator, provider: str
    ):
        return _build_device_code_token_route(authenticator, provider)

    def include_authenticator_routes(
        self,
        router: APIRouter,
        *,
        provider: str,
        authenticator: InternalAuthenticator | ExternalAuthenticator,
    ) -> None:
        for custom_router in getattr(authenticator, "include_routers", []):
            router.include_router(custom_router, prefix=f"/provider/{provider}")


def build_shared_authentication_router(
    providers: Iterable[AuthProviderRegistration],
) -> APIRouter:
    return build_authentication_router(
        providers,
        HttpServerAuthRouteAdapter(),
        external_code_methods=("GET", "POST"),
    )


build_auth_code_route = _build_auth_code_route
build_authorize_route = _build_authorize_route
build_device_code_authorize_route = _build_device_code_authorize_route
build_device_code_form_route = _build_device_code_form_route
build_device_code_submit_route = _build_device_code_submit_route
build_device_code_token_route = _build_device_code_token_route
build_handle_credentials_route = _build_handle_credentials_route

__all__ = [
    "ExternalAuthenticator",
    "InternalAuthenticator",
    "UserSessionState",
    "_extract_scopes",
    "get_current_principal",
    "get_current_principal_websocket",
    "base_authentication_router",
    "build_auth_code_route",
    "build_authorize_route",
    "build_device_code_authorize_route",
    "build_device_code_form_route",
    "build_device_code_submit_route",
    "build_device_code_token_route",
    "build_shared_authentication_router",
    "build_handle_credentials_route",
    "HttpServerAuthRouteAdapter",
    "oauth2_scheme",
]
