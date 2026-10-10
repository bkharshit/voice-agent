import asyncio

from httpx import ASGITransport, AsyncClient
from voice_agent.api import create_app

from tests.test_sessions import AnswerRuntime


async def test_api_auth_profiles_revision_and_history(system):
    settings, _, _, _, _ = system
    settings.api_key = "test-access-key-that-is-long-enough"
    app = create_app(settings, runtime_builder=lambda profile: AnswerRuntime())
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            assert (await client.get("/v1/capabilities")).status_code == 401
            client.headers["Authorization"] = "Bearer " + settings.api_key
            caps = (await client.get("/v1/capabilities")).json()
            assert caps["database"] == ("postgresql" if settings.mode == "normal" else "sqlite")
            assert "api_key" not in str(caps)
            row = (await client.post("/v1/sessions", json={"profile_id": "first"})).json()
            sid = row["id"]
            assert (await client.post(f"/v1/sessions/{sid}/turns", json={"text": "Hello"})).status_code == 202
            await asyncio.wait_for(app.state.controller.sessions[sid].task, 3)
            result = (await client.get(f"/v1/sessions/{sid}")).json()
            assert len(result["messages"]) == 2
            assert (
                await client.patch(
                    f"/v1/sessions/{sid}/configuration",
                    json={"llm_profile": "second", "expected_revision": 99},
                )
            ).status_code == 409
            ticket = (await client.post(f"/v1/sessions/{sid}/connection", json={})).json()
            assert ticket["expires_in"] == 30 and len(ticket["ticket"]) > 30
            assert (await client.delete(f"/v1/sessions/{sid}")).status_code == 204
