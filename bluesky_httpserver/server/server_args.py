import argparse
from typing import NamedTuple

def formatter(prog):
    # Set maximum width such that printed help mostly fits in the RTD theme code block (documentation).
    return argparse.RawDescriptionHelpFormatter(prog, max_help_position=20, width=90)

class ServerArgs(NamedTuple):
    public: str
    config_path: str
    http_server_port: int
    http_server_host: str

def sanitize_parsed_args(args):
    server_args = ServerArgs()
    server_args.public = args.public
    server_args.config_path = args.config_path

    server_args.http_server_host = args.http_server_host
    http_server_port = args.http_server_port
    server_args.http_server_port = int(http_server_port) if http_server_port else http_server_port

    return server_args

def server_arg_parser():
    parser = argparse.ArgumentParser(
        description="Start Bluesky HTTP Server.\n" f"bluesky-httpserver version {qserver_version}.\n",
        formatter_class=formatter,
    )

    parser.add_argument(
        "--host",
        dest="http_server_host",
        action="store",
        default=None,
        help="HTTP server host name, e.g. '127.0.0.1' or 'localhost' " f"(default: {default_http_server_host!r}).",
    )

    parser.add_argument(
        "--port",
        dest="http_server_port",
        action="store",
        default=None,
        help="HTTP server port, e.g. '127.0.0.1' or 'localhost' " f"(default: {default_http_server_port!r}).",
    )

    parser.add_argument(
        "--public",
        dest="public",
        action="store_true",
        default=False,
        help="Explicitly allows public access to the server and disables authorization/authentication.",
    )

    parser.add_argument(
        "--config_path",
        dest="config_path",
        action="store",
        default=None,
        help="Path to configuration file or directory with configuration files. The path overrides "
        "the path defined in QSERVER_HTTP_SERVER_CONFIG environment variable. If the parameter and "
        "the environemnt variable is not specified, then no configuration file is loaded.",
    )

    return sanitize_parsed_args(parser.parse_args())
