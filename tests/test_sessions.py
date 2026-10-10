import asyncio

import pytest
from voice_agent.domain import AppError, RuntimeEvent


class AnswerRuntime:
    def __init__(self, answer="A short response for the conversation."):
        self.answer = answer
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        yield RuntimeEvent(type="text", data={"text": self.answer})


class SlowRuntime:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run(self, request):
        yield RuntimeEvent(type="text", data={"text": "First part of an unfinished response. "})
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


async def finished(controller, sid):
    task = controller.sessions[sid].task
    await asyncio.wait_for(asyncio.shield(task), 3)


async def test_durable_history_and_rehydration(system):
    _, repo, _, factory, controller = system
    runtime = AnswerRuntime()
    factory.build = lambda profile: runtime
    row = await controller.create("alice", "first")
    sid = row["id"]
    await controller.turn(sid, "alice", "Hello", False)
    await finished(controller, sid)
    controller.sessions.clear()
    snapshot = await controller.snapshot(sid, "alice")
    assert [m["role"] for m in snapshot["messages"]] == ["user", "assistant"]
    assert snapshot["messages"][1]["text"] == runtime.answer
    await controller.turn(sid, "alice", "What did you say?", False)
    await finished(controller, sid)
    assert runtime.requests[1].context[1]["text"] == runtime.answer


async def test_owner_isolation_even_when_cached(system):
    *_, controller = system
    row = await controller.create("alice", "first")
    await controller.load(row["id"], "alice")
    with pytest.raises(AppError) as exc:
        await controller.snapshot(row["id"], "bob")
    assert exc.value.status == 404


async def test_interrupt_persists_partial_response_and_releases_capacity(system):
    _, repo, _, factory, controller = system
    slow = SlowRuntime()
    factory.build = lambda profile: slow
    row = await controller.create("alice", "first")
    result = await controller.turn(row["id"], "alice", "Long answer", False)
    await slow.started.wait()
    assert await controller.interrupt(row["id"], "alice", "stale") == {"interrupted": False}
    await controller.interrupt(row["id"], "alice", result["generation_id"])
    assert slow.cancelled.is_set()
    messages = await repo.history(row["id"])
    assert messages[-1]["status"] == "interrupted"
    assert messages[-1]["text"].startswith("First part")
    assert not factory.limits["first"].locked()


async def test_next_turn_switch_is_versioned_and_does_not_mutate_running_turn(system):
    _, _, _, factory, controller = system
    slow = SlowRuntime()
    factory.build = lambda profile: slow
    row = await controller.create("alice", "first")
    await controller.turn(row["id"], "alice", "Hello", False)
    await slow.started.wait()
    changed = await controller.configure(row["id"], "alice", "second", 1, "next_turn")
    assert changed["profile_id"] == "first" and changed["pending_profile"] == "second"
    assert not slow.cancelled.is_set()
    with pytest.raises(AppError) as exc:
        await controller.configure(row["id"], "alice", "second", 1, "next_turn")
    assert exc.value.status == 409
    factory.build = lambda profile: AnswerRuntime()
    await controller.turn(row["id"], "alice", "Next question", False)
    await finished(controller, row["id"])
    assert controller.sessions[row["id"]].row["profile_id"] == "second"
    events = controller.sessions[row["id"]].events
    first_delta = next(e for e in events if e.type == "text_delta")
    assert first_delta.config_revision == 1


async def test_invalid_switch_preserves_current_generation(system):
    _, _, _, factory, controller = system
    slow = SlowRuntime()
    factory.build = lambda profile: slow
    row = await controller.create("alice", "first")
    await controller.turn(row["id"], "alice", "Hello", False)
    await slow.started.wait()
    with pytest.raises(AppError):
        await controller.configure(row["id"], "alice", "missing-key", 1, "interrupt_current")
    assert not slow.cancelled.is_set()
    changed = await controller.configure(row["id"], "alice", "second", 1, "interrupt_current")
    assert slow.cancelled.is_set()
    assert changed["profile_id"] == "second"


async def test_voice_context_contains_only_delivered_speech(system):
    _, repo, _, factory, controller = system
    runtime = AnswerRuntime("First sentence is complete. Second sentence is also complete.")
    factory.build = lambda profile: runtime
    row = await controller.create("alice", "first")
    result = await controller.turn(row["id"], "alice", "Tell me", True)
    await finished(controller, row["id"])
    live = controller.sessions[row["id"]]
    segments = [e for e in live.events if e.type == "speech_segment"]
    first_end = segments[0].data["end_char"]
    assert not await controller.delivery(row["id"], "alice", result["generation_id"], 12)
    assert await controller.delivery(row["id"], "alice", result["generation_id"], first_end)
    await controller.interrupt(row["id"], "alice", result["generation_id"])
    assert (await repo.history(row["id"]))[-1]["status"] == "interrupted"
    runtime.answer = "New reply."
    await controller.turn(row["id"], "alice", "Continue", False)
    await finished(controller, row["id"])
    context = " ".join(m["text"] for m in runtime.requests[-1].context)
    assert "First sentence is complete." in context
    assert "Second sentence" not in context


async def test_voice_backpressure_and_duplicate_receipts(system):
    _, _, _, factory, controller = system
    factory.build = lambda profile: AnswerRuntime("One sufficiently long sentence. " * 6)
    row = await controller.create("alice", "first")
    result = await controller.turn(row["id"], "alice", "Hello", True)
    live = controller.sessions[row["id"]]
    for _ in range(100):
        segments = [e for e in live.events if e.type == "speech_segment"]
        if len(segments) == 2:
            break
        await asyncio.sleep(0.01)
    assert len(segments) == 2
    assert not live.task.done()
    end = segments[0].data["end_char"]
    await controller.delivery(row["id"], "alice", result["generation_id"], end)
    await controller.delivery(row["id"], "alice", result["generation_id"], end)
    await asyncio.sleep(0.03)
    assert len([e for e in live.events if e.type == "speech_segment"]) == 3
    await controller.interrupt(row["id"], "alice", result["generation_id"])


async def test_completed_tool_facts_survive_interrupt_and_restart(system):
    _, repo, _, _, controller = system
    row = await controller.create("alice", "first")
    await repo.tool_start("completed-tool", row["id"], "generation", "calculate", {"left": 1})
    await repo.tool_end("completed-tool", "succeeded", {"result": 2})
    await repo.tool_start("unknown-tool", row["id"], "generation", "lookup", {})
    await repo.recover()
    live = await controller.load(row["id"], "alice")
    context = await controller._context(live, 1000)
    assert "succeeded" in context[-1]["text"] and "outcome_unknown" in context[-1]["text"]


async def test_closed_session_rejects_resume(system):
    *_, controller = system
    row = await controller.create("alice", "first")
    await controller.close_session(row["id"], "alice")
    with pytest.raises(AppError) as exc:
        await controller.load(row["id"], "alice")
    assert exc.value.status == 410


async def test_busy_model_rejects_without_creating_a_turn(system):
    _, repo, _, factory, controller = system
    slow = SlowRuntime()
    factory.build = lambda profile: slow
    first = await controller.create("alice", "first")
    second = await controller.create("bob", "first")
    await controller.turn(first["id"], "alice", "Hello", False)
    await slow.started.wait()
    with pytest.raises(AppError) as exc:
        await controller.turn(second["id"], "bob", "Hello", False)
    assert exc.value.status == 429
    assert await repo.history(second["id"]) == []
