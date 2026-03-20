import os
import sys
import time as ttime

import pytest
import requests
from bluesky_queueserver.manager.tests.common import set_qserver_zmq_encoding  # noqa: F401
from bluesky_queueserver.manager.tests.common import (
    ReManager,
    condition_manager_idle,
    wait_for_condition,
    zmq_secure_request,
)
from xprocess import ProcessStarter

import bluesky_httpserver.server as bqss

SERVER_ADDRESS = "localhost"
SERVER_PORT = "60610"


def _worker_index():
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    if worker.startswith("gw"):
        try:
            return int(worker[2:])
        except ValueError:
            return 0
    return 0


def _worker_name():
    return os.environ.get("PYTEST_XDIST_WORKER", "local")


def _ports_for_worker():
    # Avoid the queue-server default ports (60615/60625), which may already be
    # occupied in shared/dev environments.
    base = 62000 + _worker_index() * 100
    return {
        "server_port": str(base + 10),
        "zmq_control_server": f"tcp://*:{base + 15}",
        "zmq_control_client": f"tcp://localhost:{base + 15}",
        "zmq_info_server": f"tcp://*:{base + 25}",
        "zmq_info_client": f"tcp://localhost:{base + 25}",
    }


def _redis_name_prefix(*, scope, sequence=None):
    parts = ["qs_unit_tests_httpserver", _worker_name(), scope]
    if sequence is not None:
        parts.append(str(sequence))
    return "_".join(parts)


def _get_cli_option_value(params, option):
    option_with_eq = f"{option}="
    for n, value in enumerate(params):
        if value.startswith(option_with_eq):
            return value[len(option_with_eq) :]
        if value == option and n + 1 < len(params):
            return params[n + 1]
    return None


def _server_to_client_zmq_addr(addr):
    if addr.startswith("tcp://*:"):
        return f"tcp://localhost:{addr.rsplit(':', 1)[1]}"
    if addr.startswith("tcp://0.0.0.0:"):
        return f"tcp://localhost:{addr.rsplit(':', 1)[1]}"
    return addr


def _set_zmq_env(control_addr, info_addr):
    os.environ["QSERVER_ZMQ_CONTROL_ADDRESS"] = control_addr
    os.environ["QSERVER_ZMQ_INFO_ADDRESS"] = info_addr
    os.environ["_TEST_QSERVER_ZMQ_ADDRESS_"] = control_addr


def _ensure_manager_addresses_in_params(params):
    ports = _ports_for_worker()

    control_addr = _get_cli_option_value(params, "--zmq-control-addr")
    if control_addr is None:
        control_addr = ports["zmq_control_server"]
        params.append(f"--zmq-control-addr={control_addr}")

    info_addr = _get_cli_option_value(params, "--zmq-info-addr")
    if info_addr is None:
        info_addr = ports["zmq_info_server"]
        params.append(f"--zmq-info-addr={info_addr}")

    return {
        "control_server": control_addr,
        "control_client": _server_to_client_zmq_addr(control_addr),
        "info_server": info_addr,
        "info_client": _server_to_client_zmq_addr(info_addr),
    }


def _xprocess_name(name):
    worker = os.environ.get("PYTEST_XDIST_WORKER", "local")
    return f"{name}_{worker}"


def pytest_configure(config):
    del config
    global SERVER_PORT
    ports = _ports_for_worker()
    SERVER_PORT = ports["server_port"]
    os.environ["QSERVER_ZMQ_CONTROL_ADDRESS"] = ports["zmq_control_client"]
    os.environ["QSERVER_ZMQ_INFO_ADDRESS"] = ports["zmq_info_client"]
    os.environ["_TEST_QSERVER_ZMQ_ADDRESS_"] = ports["zmq_control_client"]


def _wait_for_manager_ready(timeout=10):
    if not wait_for_condition(time=timeout, condition=condition_manager_idle):
        raise TimeoutError("Timeout: RE Manager failed to start.")


def _reset_queue_mode():
    resp, msg = zmq_secure_request("queue_mode_set", params={"mode": "default"})
    if not resp or resp.get("success") is not True:
        raise RuntimeError(msg)


def _reset_queue_mode_and_clear_queue():
    _reset_queue_mode()

    resp, msg = zmq_secure_request("queue_clear")
    if not resp or resp.get("success") is not True:
        raise RuntimeError(msg)


# Single-user API key used for most of the tests
API_KEY_FOR_TESTS = "APIKEYFORTESTS"

_user_group = "primary"


def _wait_for_http_server_ready(*, timeout=10, request_prefix="/api"):
    """Wait until HTTP server accepts connections and responds to /status."""
    t_stop = ttime.time() + timeout
    url = f"http://{SERVER_ADDRESS}:{SERVER_PORT}{request_prefix}/status"
    while ttime.time() < t_stop:
        try:
            response = requests.get(url, timeout=0.5)
            # Any HTTP response means the server is up (auth may still reject request).
            if response.status_code:
                return
        except requests.RequestException:
            pass
        ttime.sleep(0.1)
    raise TimeoutError(f"HTTP server is not ready after {timeout} s: {url}")


@pytest.fixture(scope="module")
def fastapi_server(xprocess):
    class Starter(ProcessStarter):
        env = dict(os.environ)
        env["QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY"] = API_KEY_FOR_TESTS

        pattern = "Bluesky HTTP Server started successfully"
        args = f"uvicorn --host={SERVER_ADDRESS} --port {SERVER_PORT} {bqss.__name__}:app".split()
        # args = f"start-bluesky-httpserver --host={SERVER_ADDRESS} --port {SERVER_PORT}".split()

    proc_name = _xprocess_name("fastapi_server")
    xprocess.ensure(proc_name, Starter)
    _wait_for_http_server_ready()

    yield

    xprocess.getinfo(proc_name).terminate()


@pytest.fixture
def fastapi_server_fs(xprocess):
    """
    FastAPI server with function scope. Should not be executed in the same module as ``fastapi_server``.
    The server must be explicitly started in the unit test code as ``fastapi_server_fs()``. This allows
    to perform additional steps (such as setting environmental variables) before the server is started.
    """

    def start(
        http_server_host=SERVER_ADDRESS,
        http_server_port=SERVER_PORT,
        api_key=API_KEY_FOR_TESTS,
    ):
        class Starter(ProcessStarter):
            max_read_lines = 53

            env = dict(os.environ)
            if api_key:
                env["QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY"] = api_key

            pattern = "Bluesky HTTP Server started successfully"
            args = f"uvicorn --host={http_server_host} --port {http_server_port} {bqss.__name__}:app".split()

        proc_name = _xprocess_name("fastapi_server")
        xprocess.ensure(proc_name, Starter)
        _wait_for_http_server_ready()

    yield start

    xprocess.getinfo(_xprocess_name("fastapi_server")).terminate()


@pytest.fixture
def re_manager():  # noqa: F811
    ports = _ports_for_worker()

    _set_zmq_env(ports["zmq_control_client"], ports["zmq_info_client"])

    manager = ReManager(
        params=[
            f"--zmq-control-addr={ports['zmq_control_server']}",
            f"--zmq-info-addr={ports['zmq_info_server']}",
            f"--redis-name-prefix={_redis_name_prefix(scope='re_manager')}",
        ],
        set_redis_name_prefix=False,
    )
    failed_to_start = False

    try:
        _wait_for_manager_ready()
        _reset_queue_mode_and_clear_queue()
        yield manager
    except Exception:
        failed_to_start = True
        raise
    finally:
        if failed_to_start:
            manager.kill_manager()
        else:
            try:
                manager.stop_manager(timeout=30)
            except Exception:
                manager.kill_manager()


@pytest.fixture(scope="module")
def re_manager_module():
    ports = _ports_for_worker()
    _set_zmq_env(ports["zmq_control_client"], ports["zmq_info_client"])

    manager = ReManager(
        params=[
            f"--zmq-control-addr={ports['zmq_control_server']}",
            f"--zmq-info-addr={ports['zmq_info_server']}",
            f"--redis-name-prefix={_redis_name_prefix(scope='re_manager_module')}",
        ],
        set_redis_name_prefix=False,
    )
    failed_to_start = False

    try:
        _wait_for_manager_ready()
        _reset_queue_mode_and_clear_queue()
        yield manager
    except Exception:
        failed_to_start = True
        raise
    finally:
        if failed_to_start:
            manager.kill_manager()
        else:
            try:
                manager.stop_manager(timeout=30)
            except Exception:
                manager.kill_manager()


@pytest.fixture
def re_manager_cmd():  # noqa: F811
    manager = None
    failed_to_start = False
    manager_sequence = 0

    def _close_manager():
        nonlocal manager, failed_to_start

        if manager is None:
            return

        if failed_to_start:
            try:
                manager.kill_manager()
            except Exception:
                pass
            manager = None
            return

        try:
            manager.stop_manager(timeout=30)
        except Exception:
            try:
                manager.kill_manager()
            except Exception:
                pass
        finally:
            manager = None

    def create_re_manager(
        params=None, *, stdout=sys.stdout, stderr=sys.stdout, set_redis_name_prefix=True
    ):
        nonlocal manager, failed_to_start, manager_sequence

        failed_to_start = False
        manager_sequence += 1

        _close_manager()

        params = list(params or [])
        addrs = _ensure_manager_addresses_in_params(params)
        _set_zmq_env(addrs["control_client"], addrs["info_client"])

        # Always force per-worker/per-create Redis prefixes to avoid collisions in parallel runs.
        if _get_cli_option_value(params, "--redis-name-prefix") is None:
            params.append(
                f"--redis-name-prefix={_redis_name_prefix(scope='re_manager_cmd', sequence=manager_sequence)}"
            )
        # We explicitly manage the Redis name prefix and do not want defaults from upstream fixture logic.
        set_redis_name_prefix = False

        manager = ReManager(
            params=params,
            stdout=stdout,
            stderr=stderr,
            set_redis_name_prefix=set_redis_name_prefix,
        )

        if not wait_for_condition(time=10, condition=condition_manager_idle):
            failed_to_start = True
            manager.kill_manager()
            raise TimeoutError("Timeout: RE Manager failed to start.")

        _reset_queue_mode()
        return manager

    yield create_re_manager

    _close_manager()


def setup_server_with_config_file(*, config_file_str, tmpdir, monkeypatch):
    """
    Creates config file for the server in ``tmpdir/config/`` directory and
    sets up the respective environment variable. Sets ``tmpdir`` as a current directory.
    """
    print(f"SERVER CONFIGURATION:\n{'-' * 50}\n{config_file_str}\n{'-' * 50}")
    config_fln = "config_httpserver.yml"
    config_dir = os.path.join(tmpdir, "config")
    config_path = os.path.join(config_dir, config_fln)
    os.makedirs(config_dir)
    with open(config_path, "wt") as f:
        f.writelines(config_file_str)

    sqlite_path = os.path.join(tmpdir, "bluesky_httpserver.sqlite")
    sqlite_path = "sqlite:///" + sqlite_path

    monkeypatch.setenv("QSERVER_HTTP_SERVER_CONFIG", config_path)
    monkeypatch.setenv("QSERVER_HTTP_SERVER_DATABASE_URI", sqlite_path)
    monkeypatch.chdir(tmpdir)

    return config_path


def add_plans_to_queue():
    """
    Clear the queue and add 3 fixed plans to the queue.
    Raises an exception if clearing the queue or adding plans fails.
    """
    resp1, _ = zmq_secure_request("queue_clear")
    assert resp1 and (resp1.get("success") is True), str(resp1)

    user_group = _user_group
    user = "HTTP unit test setup"
    plan1 = {
        "name": "count",
        "args": [["det1", "det2"]],
        "kwargs": {"num": 10, "delay": 1},
        "item_type": "plan",
    }
    plan2 = {"name": "count", "args": [["det1", "det2"]], "item_type": "plan"}
    for plan in (plan1, plan2, plan2):
        resp2, _ = zmq_secure_request(
            "queue_item_add", {"item": plan, "user": user, "user_group": user_group}
        )
        assert resp2 and (resp2.get("success") is True), str(resp2)


def request_to_json(
    request_type,
    path,
    *,
    request_prefix="/api",
    api_key=API_KEY_FOR_TESTS,
    token=None,
    login=None,
    **kwargs,
):
    if login:
        auth = None
        data = {"username": login[0], "password": login[1]}
        kwargs.setdefault("data", {})
        kwargs.update({"data": data})
    elif token:
        auth = None
        headers = {"Authorization": f"Bearer {token}"}
        kwargs.update({"auth": auth, "headers": headers})
    elif api_key:
        auth = None
        headers = {"Authorization": f"ApiKey {api_key}"}
        kwargs.update({"auth": auth, "headers": headers})

    method = getattr(requests, request_type)
    resp = method(
        f"http://{SERVER_ADDRESS}:{SERVER_PORT}{request_prefix}{path}", **kwargs
    )
    resp = resp.json()
    return resp


def wait_for_environment_to_be_created(
    timeout, polling_period=0.2, api_key=API_KEY_FOR_TESTS
):
    """Wait for environment to be created with timeout."""
    time_start = ttime.time()
    while ttime.time() < time_start + timeout:
        ttime.sleep(polling_period)
        resp = request_to_json("get", "/status", api_key=api_key)
        if resp["worker_environment_exists"] and (resp["manager_state"] == "idle"):
            return True

    return False


def wait_for_environment_to_be_closed(
    timeout, polling_period=0.2, api_key=API_KEY_FOR_TESTS
):
    """Wait for environment to be closed with timeout."""
    time_start = ttime.time()
    while ttime.time() < time_start + timeout:
        ttime.sleep(polling_period)
        resp = request_to_json("get", "/status", api_key=api_key)
        if (not resp["worker_environment_exists"]) and (
            resp["manager_state"] == "idle"
        ):
            return True

    return False


def wait_for_queue_execution_to_complete(
    timeout, polling_period=0.2, api_key=API_KEY_FOR_TESTS
):
    """Wait for for queue execution to complete."""
    time_start = ttime.time()
    while ttime.time() < time_start + timeout:
        ttime.sleep(polling_period)
        resp = request_to_json("get", "/status", api_key=api_key)
        if (resp["manager_state"] == "idle") and (resp["items_in_queue"] == 0):
            return True

    return False


def wait_for_manager_state_idle(timeout, polling_period=0.2, api_key=API_KEY_FOR_TESTS):
    """Wait until manager is in 'idle' state."""
    time_start = ttime.time()
    while ttime.time() < time_start + timeout:
        ttime.sleep(polling_period)
        resp = request_to_json("get", "/status", api_key=api_key)
        if resp["manager_state"] == "idle":
            return True

    return False


def wait_for_manager_state_idle_or_paused(
    timeout, polling_period=0.2, api_key=API_KEY_FOR_TESTS
):
    """Wait until manager is in 'idle' state."""
    time_start = ttime.time()
    while ttime.time() < time_start + timeout:
        ttime.sleep(polling_period)
        resp = request_to_json("get", "/status", api_key=api_key)
        if resp["manager_state"] in ("idle", "paused"):
            return True

    return False


def wait_for_ip_kernel_idle(timeout, polling_period=0.2, api_key=API_KEY_FOR_TESTS):
    """Wait until manager is in 'idle' state."""
    time_start = ttime.time()
    while ttime.time() < time_start + timeout:
        ttime.sleep(polling_period)
        resp = request_to_json("get", "/status", api_key=api_key)
        if resp["ip_kernel_state"] == "idle":
            return True

    return False


# ============================================================================
# OIDC Test Fixtures
# ============================================================================


@pytest.fixture
def oidc_base_url() -> str:
    """Base URL for mock OIDC provider."""
    return "https://example.com/realms/example/"


@pytest.fixture
def well_known_response(oidc_base_url: str) -> dict:
    """Mock OIDC well-known configuration response."""
    return {
        "id_token_signing_alg_values_supported": ["RS256"],
        "issuer": oidc_base_url.rstrip("/"),
        "jwks_uri": f"{oidc_base_url}protocol/openid-connect/certs",
        "authorization_endpoint": f"{oidc_base_url}protocol/openid-connect/auth",
        "token_endpoint": f"{oidc_base_url}protocol/openid-connect/token",
        "device_authorization_endpoint": f"{oidc_base_url}protocol/openid-connect/auth/device",
        "end_session_endpoint": f"{oidc_base_url}protocol/openid-connect/logout",
    }
