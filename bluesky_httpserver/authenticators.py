import warnings

from bluesky_authentication.authenticators import (  # noqa: F401
    DictionaryAuthenticator,
    DummyAuthenticator,
    EntraAuthenticator,
    LDAPAuthenticator,
    OIDCAuthenticator,
    PAMAuthenticator,
    ProxiedOIDCAuthenticator,
    SAMLAuthenticator,
)
from bluesky_authentication.protocols import (  # noqa: F401
    ExternalAuthenticator,
    InternalAuthenticator,
    UserSessionState,
)

warnings.warn(
    "Importing authenticators from 'bluesky_httpserver.authenticators' is deprecated "
    "and will be removed in a future release. Use 'bluesky_authentication.authenticators' "
    "and 'bluesky_authentication.protocols' instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "DictionaryAuthenticator",
    "DummyAuthenticator",
    "EntraAuthenticator",
    "ExternalAuthenticator",
    "InternalAuthenticator",
    "LDAPAuthenticator",
    "OIDCAuthenticator",
    "PAMAuthenticator",
    "ProxiedOIDCAuthenticator",
    "SAMLAuthenticator",
    "UserSessionState",
]
