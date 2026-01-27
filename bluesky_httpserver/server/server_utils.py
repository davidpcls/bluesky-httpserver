from .utils import get_authenticators
from .settings import get_settings

def print_admin_api_key_if_generated(web_app, host, port):
    # host = host or "127.0.0.1"
    # port = port or 8000
    settings = web_app.dependency_overrides.get(get_settings, get_settings)()

    logger.info("APP settings: %s", pprint.pformat(dict(settings)))
    authenticators = web_app.dependency_overrides.get(get_authenticators, get_authenticators)()
    if settings.allow_anonymous_access:
        print(
            "The server is running in 'public' mode, permitting open, anonymous access\n"
            "for reading. Any data that is not specifically controlled with an access\n"
            "policy will be visible to anyone who can connect to this server.\n",
            file=sys.stderr,
        )
    if (not authenticators) and settings.single_user_api_key_generated:
        print(
            "Navigate a web browser to:\n\n"
            f"http://{host}:{port}?api_key={settings.single_user_api_key}\n\n"
            "or connect an HTTP client to:\n\n"
            f"http://{host}:{port}/api?api_key={settings.single_user_api_key}\n",
            file=sys.stderr,
        )
