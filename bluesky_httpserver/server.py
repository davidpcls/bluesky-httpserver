import logging
import os
import pprint
import sys
from pathlib import Path

import bluesky_httpserver

from .app import build_app
from .config import construct_build_app_kwargs, parse_configs
from .server.server_args import server_arg_parser
from .server.server_utils import print_admin_api_key_if_generated

logger = logging.getLogger(__name__)

qserver_version = bluesky_httpserver.__version__

default_http_server_host = "localhost"
default_http_server_port = 60610


def start_server():
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("bluesky_httpserver").setLevel("INFO")

    args = server_arg_parser()

    logger.info("Preparing to start Bluesky HTTP Server ...")

    config_path = args.config_path or os.getenv("QSERVER_HTTP_SERVER_CONFIG", None)
    try:
        parsed_config = parse_configs(config_path) if config_path else {}
    except Exception as ex:
        logger.error(ex)
        raise

    # Let --public flag override settings in config.
    if args.public:
        if "authentication" not in parsed_config:
            parsed_config["authentication"] = {}
        parsed_config["authentication"]["allow_anonymous_access"] = True

    # Extract config for uvicorn.
    uvicorn_kwargs = parsed_config.pop("uvicorn", {})
    # 'host' and 'port' from CLI parameters overrides the parameters from config.
    uvicorn_kwargs["host"] = args.http_server_host or uvicorn_kwargs.get("host", default_http_server_host)
    uvicorn_kwargs["port"] = args.http_server_port or uvicorn_kwargs.get("port", default_http_server_port)

    # This config was already validated when it was parsed. Do not re-validate.
    kwargs = construct_build_app_kwargs(parsed_config, source_filepath=config_path)
    if config_path:
        logger.info(f"Using configuration from {Path(config_path).absolute()}")
    else:
        logger.info("No configuration file was specified. Using CLI parameters and environment variables.")

    web_app = build_app(**kwargs)
    print_admin_api_key_if_generated(web_app, host=uvicorn_kwargs["host"], port=uvicorn_kwargs["port"])

    logger.info("Starting Bluesky HTTP Server at {http_server_host}:{http_server_port} ...")

    import uvicorn

    uvicorn.run(web_app, **uvicorn_kwargs)

def app_factory():
    """
    Return an ASGI app instance.

    Use a configuration file at the path specified by the environment variable
    QSERVER_HTTP_SERVER_CONFIG. If the env. variable is not set, then do not load
    configuration.

    This is intended to be used for horizontal deployment (using gunicorn, for
    example) where only a module and instance or factory can be specified.
    """
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("bluesky_httpserver").setLevel("INFO")

    config_path = os.getenv("QSERVER_HTTP_SERVER_CONFIG", None)

    from .config import construct_build_app_kwargs, parse_configs

    try:
        parsed_config = parse_configs(config_path) if config_path else {}
    except Exception as ex:
        logger.error(ex)
        raise

    # This config was already validated when it was parsed. Do not re-validate.
    kwargs = construct_build_app_kwargs(parsed_config, source_filepath=config_path)
    if config_path:
        logger.info(f"Using configuration from {Path(config_path).absolute()}")
    else:
        logger.info("No configuration file was specified. Using environment variables.")

    web_app = build_app(**kwargs)
    uvicorn_config = parsed_config.get("uvicorn", {})
    print_admin_api_key_if_generated(web_app, host=uvicorn_config.get("host"), port=uvicorn_config.get("port"))

    return web_app


def __getattr__(name):
    """
    This supports tiled.server.app.app by creating app on demand.
    """
    if name == "app":
        try:
            return app_factory()
        except Exception as err:
            raise Exception("Failed to create app.") from err
    raise AttributeError(name)
