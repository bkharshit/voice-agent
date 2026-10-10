import asyncio

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from voice_agent.agents import AdkRuntime
from voice_agent.domain import TurnRequest, new_id


class ToolModel(BaseLlm):
    model: str = "test-tool-model"

    async def generate_content_async(self, llm_request, stream=False):
        tool_result = any(
            part.function_response for content in llm_request.contents for part in content.parts or []
        )
        if tool_result:
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part(text="The answer is 5.")])
            )
        else:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                id="call_1",
                                name="calculate",
                                args={"left": 2, "operation": "add", "right": 3},
                            )
                        )
                    ],
                )
            )


async def test_real_adk_runner_executes_and_records_native_tool(system, monkeypatch):
    _, repo, catalog, _, controller = system
    monkeypatch.setattr("google.adk.models.lite_llm.LiteLlm", lambda **kwargs: ToolModel())
    row = await controller.create("alice", "first")
    runtime = AdkRuntime(catalog.get("first"), catalog, repo)
    request = TurnRequest(session_id=row["id"], generation_id=new_id(), text="Add 2 and 3", context=[])
    async with asyncio.timeout(10):
        events = [event async for event in runtime.run(request)]
    assert [event.type for event in events] == ["tool_started", "tool_completed", "text"]
    assert events[-1].data["text"] == "The answer is 5."
    tools = await repo.tools(row["id"])
    assert tools[0]["status"] == "succeeded" and tools[0]["result"] == {"result": 5.0}
