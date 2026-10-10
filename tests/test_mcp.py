import asyncio
import os

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from voice_agent.agents import AdkRuntime
from voice_agent.domain import TurnRequest, new_id
from voice_agent.settings import McpServer


class McpModel(BaseLlm):
    model: str = "mcp-test"

    async def generate_content_async(self, llm_request, stream=False):
        exposed = [
            declaration.name
            for tool in llm_request.config.tools or []
            for declaration in tool.function_declarations or []
        ]
        assert "test_echo" in exposed
        assert "test_not_allowed" not in exposed
        if any(part.function_response for c in llm_request.contents for part in c.parts or []):
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="Hello from MCP.")]))
        else:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                id="mcp_1", name="test_echo", args={"message": "hello"}
                            )
                        )
                    ],
                )
            )


async def test_mcp_allowlist_real_connection_and_durable_result(system, monkeypatch):
    if not os.getenv("TEST_MCP_URL"):
        pytest.skip("Set TEST_MCP_URL to the local fixture server")
    _, repo, catalog, _, controller = system
    catalog.mcp = [McpServer(id="test", url=os.environ["TEST_MCP_URL"], allowed_tools=["echo"])]
    monkeypatch.setattr("google.adk.models.lite_llm.LiteLlm", lambda **kwargs: McpModel())
    row = await controller.create("alice", "first")
    runtime = AdkRuntime(catalog.get("first"), catalog, repo)
    request = TurnRequest(session_id=row["id"], generation_id=new_id(), text="Echo hello", context=[])
    async with asyncio.timeout(10):
        events = [event async for event in runtime.run(request)]
    assert any(e.type == "tool_completed" and e.data["name"] == "test_echo" for e in events)
    assert (await repo.tools(row["id"]))[0]["status"] == "succeeded"
