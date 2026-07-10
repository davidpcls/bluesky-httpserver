import socket
import time

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network


def wait_for_port(host: str, port: int, timeout: float = 30.0):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, int(port)), timeout=1):
                return
        except OSError:
            time.sleep(0.2)

    raise TimeoutError(f"Timed out waiting for {host}:{port}")


@pytest.fixture(scope="session")
def queue_server_infra():
    with Network() as network:

        redis = (
            DockerContainer("redis:7-alpine")
            .with_network(network)
            .with_network_aliases("redis")
        )

        with redis:

            qserver = (
                DockerContainer("test-queueserver")
                .with_network(network)
                .with_env("REDIS_ADDR", "redis:6379")
                .with_exposed_ports(60615, 60620)
            )

            with qserver:

                control_port = int(qserver.get_exposed_port(60615))
                info_port = int(qserver.get_exposed_port(60620))

                wait_for_port("localhost", control_port)
                wait_for_port("localhost", info_port)

                yield {
                    "control": f"tcp://localhost:{control_port}",
                    "info": f"tcp://localhost:{info_port}",
                }

@pytest_asyncio.fixture
async def client(queue_server_infra, monkeypatch):

    monkeypatch.setenv(
        "QSERVER_ZMQ_CONTROL_ADDRESS",
        queue_server_infra["control"],
    )
    monkeypatch.setenv(
        "QSERVER_ZMQ_INFO_ADDRESS",
        queue_server_infra["info"],
    )
    monkeypatch.setenv(
        "QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY",
        "test-key",
    )

    from bluesky_httpserver.server import build_app
    app = build_app(
        server_settings={
            "server_configuration": {}
        }
)
    # app = build_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "ApiKey test-key"},
    ) as client:
        yield client

# import os
# import time
# import pytest
# import asyncio
# import pytest_asyncio
# from httpx import AsyncClient, ASGITransport
# from testcontainers.core.container import DockerContainer
# from testcontainers.core.network import Network
#
# # Mark the whole file to support async fixtures if using older pytest-asyncio
# pytestmark = pytest.mark.asyncio
#
# @pytest.fixture(scope="session")
# def queue_server_infra():
#     """
#     Spins up isolated Redis and Bluesky-QueueServer containers in a virtual network.
#     """
#     with Network() as network:
#         with DockerContainer("redis:7-alpine").with_network(network) as redis:
#             with (DockerContainer("test-queueserver")
#                   .with_name("queueserver-test")
#                   .with_network(network)
#                   .with_env("REDIS_ADDR", "redis-test:6379")
#                   .with_exposed_ports(60615, 60620)) as qserver:
#
#                 time.sleep(3)
#
#                 zmq_control_port = qserver.get_exposed_port(60615)
#                 zmq_console_port = qserver.get_exposed_port(60620)
#
#                 yield {
#                     "zmq_control_address": f"tcp://localhost:{zmq_control_port}",
#                     "zmq_console_address": f"tcp://localhost:{zmq_console_port}"
#                 }

# @pytest_asyncio.fixture(loop_scope="function") 
# async def integration_client(queue_server_infra):
#     """Configures bluesky-httpserver to talk to our running containers."""
#     # Define a clean, explicit API Key for Single-User Mode
#     api_key = "test-secret-key"
#
#     # Inject variables into the environment block
#     os.environ["QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY"] = api_key  # Corrected key variable name
#     os.environ["QSERVER_ZMQ_CONTROL_ADDRESS"] = queue_server_infra["zmq_control_address"]
#     os.environ["QSERVER_ZMQ_INFO_ADDRESS"] = queue_server_infra["zmq_console_address"]
#
#     from bluesky_httpserver.server import app
#
#    # Configure the client globally with the correct Authorization header format
#     headers = {"Authorization": f"ApiKey {api_key}"}
#
#     async with AsyncClient(
#         transport=ASGITransport(app=app), 
#         base_url="http://test", 
#         headers=headers
#     ) as client:
#
#         # Handshake routine using the newly corrected /api/status route
#         for _ in range(20):
#             try:
#                 res = await client.get("/api/status")
#                 if res.status_code == 200:
#                     break
#             except Exception:
#                 pass
#             await asyncio.sleep(0.5)
#
#         yield client 
#     del os.environ["QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY"]
#     del os.environ["QSERVER_ZMQ_CONTROL_ADDRESS"]
#     del os.environ["QSERVER_ZMQ_INFO_ADDRESS"]
