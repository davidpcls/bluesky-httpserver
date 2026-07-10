import pytest
from httpx import AsyncClient, ASGITransport

pytestmark = pytest.mark.asyncio

@pytest.mark.asyncio
async def test_status(client):

    response = await client.get("/api/status")

    assert response.status_code == 200

    data = response.json()

    assert "items_in_queue" in data
    assert "worker_environment_exists" in data

# @pytest.mark.asyncio
# async def test_add_plan(client):
#
#     response = await client.post(
#         "/api/queue/item/add",
#         json={
#             "name": "count",
#             "args": [],
#             "kwargs": {},
#         },
#     )
#
#     assert response.status_code == 200
#
#     body = response.json()
#
#     assert body["success"] is True
#
#
# @pytest.mark.asyncio
# async def test_add_plan_changes_queue(client):
#
#     await client.post(
#         "/api/queue/item/add",
#         json={
#             "name": "count",
#             "args": [],
#             "kwargs": {},
#         },
#     )
#
#     response = await client.get("/api/queue")
#
#     queue = response.json()["items"]
#
#     assert len(queue) == 1
#     assert queue[0]["name"] == "count"
#
# @pytest.mark.asyncio
# async def test_requires_api_key(queue_server_infra, monkeypatch):
#
#     monkeypatch.setenv(
#         "QSERVER_ZMQ_CONTROL_ADDRESS",
#         queue_server_infra["control"],
#     )
#     monkeypatch.setenv(
#         "QSERVER_ZMQ_INFO_ADDRESS",
#         queue_server_infra["info"],
#     )
#     monkeypatch.setenv(
#         "QSERVER_HTTP_SERVER_SINGLE_USER_API_KEY",
#         "secret",
#     )
#
#     from bluesky_httpserver.server import create_app
#
#     app = create_app()
#
#     async with AsyncClient(
#         transport=ASGITransport(app=app),
#         base_url="http://test",
#     ) as client:
#
#         response = await client.get("/api/status")
#
#     assert response.status_code == 401
