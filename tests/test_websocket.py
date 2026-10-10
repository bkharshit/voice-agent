import asyncio

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from voice_agent.api import create_app

from tests.test_sessions import AnswerRuntime


async def test_connection_ticket_origin_and_stream(system):
    settings, _, _, _, _ = system
    app = create_app(settings, runtime_builder=lambda profile: AnswerRuntime())

    def exercise():
        with TestClient(app) as client:
            sid = client.post("/v1/sessions", json={"profile_id": "first"}).json()["id"]
            ticket = client.post(f"/v1/sessions/{sid}/connection", json={}).json()["ticket"]
            # A forbidden browser origin cannot consume a valid ticket.
            try:
                with client.websocket_connect(
                    f"/v1/sessions/{sid}/events?ticket={ticket}",
                    headers={"origin": "https://untrusted.example"},
                ):
                    raise AssertionError("Forbidden origin connected")
            except WebSocketDisconnect as exc:
                assert exc.code == 1008
            with client.websocket_connect(
                f"/v1/sessions/{sid}/events?ticket={ticket}", headers={"origin": "http://localhost:8000"}
            ) as ws:
                assert ws.receive_json()["type"] == "connected"
                assert client.post(f"/v1/sessions/{sid}/turns", json={"text": "Hello"}).status_code == 202
                types = []
                while True:
                    event = ws.receive_json()
                    types.append(event["type"])
                    if event["type"] == "response_completed":
                        break
                assert "turn_committed" in types and "text_delta" in types
            try:
                with client.websocket_connect(f"/v1/sessions/{sid}/events?ticket={ticket}"):
                    raise AssertionError("Used ticket connected")
            except WebSocketDisconnect as exc:
                assert exc.code == 1008

    await asyncio.to_thread(exercise)


async def test_reconnect_replaces_stale_socket_without_clearing_successor(system):
    settings, _, _, _, _ = system
    app = create_app(settings, runtime_builder=lambda profile: AnswerRuntime())

    def exercise():
        with TestClient(app) as client:
            sid = client.post("/v1/sessions", json={"profile_id": "first"}).json()["id"]

            def path():
                ticket = client.post(f"/v1/sessions/{sid}/connection", json={}).json()["ticket"]
                return f"/v1/sessions/{sid}/events?ticket={ticket}"

            with client.websocket_connect(path()) as old:
                assert old.receive_json()["type"] == "connected"
                with client.websocket_connect(path()) as new:
                    assert new.receive_json()["type"] == "connected"
                    try:
                        old.receive_json()
                        raise AssertionError("Old client was not disconnected")
                    except WebSocketDisconnect as exc:
                        assert exc.code == 4001
                    new.send_json({"type": "ping"})
                    assert new.receive_json()["type"] == "pong"
                    client.post(f"/v1/sessions/{sid}/turns", json={"text": "Hello"})
                    while new.receive_json()["type"] != "response_completed":
                        pass
                    assert app.state.controller.sessions[sid].queue is not None

    await asyncio.to_thread(exercise)
