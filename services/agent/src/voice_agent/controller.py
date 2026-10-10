"""Session use cases. A per-session lock serializes commands and configuration changes."""

import asyncio
import base64
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field

from voice_agent.agents import RuntimeFactory
from voice_agent.domain import AppError, Event, TurnRequest, new_id
from voice_agent.persistence import Repository
from voice_agent.settings import Catalog, Settings
from voice_agent.speech import PhraseBuffer

logger = logging.getLogger(__name__)


@dataclass
class LiveSession:
    row: dict
    history: list[dict]
    owner: str = "local"
    tool_facts: list[dict] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    generation: str | None = None
    turn: str | None = None
    task: asyncio.Task | None = None
    queue: asyncio.Queue | None = None
    close_connection: Callable[[], Awaitable[None]] | None = None
    sequence: int = 0
    output: str = "idle"
    touched: float = field(default_factory=time.monotonic)
    events: deque = field(default_factory=lambda: deque(maxlen=256))
    allowed_receipts: dict[str, set[int]] = field(default_factory=dict)
    playback_slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(2))
    received_chars: dict[str, int] = field(default_factory=dict)
    generation_revision: int = 1


class SessionController:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        catalog: Catalog,
        runtime_factory: RuntimeFactory,
        audio=None,
    ):
        self.settings = settings
        self.repository = repository
        self.catalog = catalog
        self.factory = runtime_factory
        self.audio = audio
        self.sessions: dict[str, LiveSession] = {}
        self.hydration_lock = asyncio.Lock()

    async def load(self, session_id: str, owner: str) -> LiveSession:
        cached = self.sessions.get(session_id)
        if cached:
            if cached.owner != owner:
                raise AppError("Session not found", 404)
            cached.touched = time.monotonic()
            return cached
        row = await self.repository.get(session_id, owner)
        if row["status"] != "active":
            raise AppError("Session is closed", 410)
        async with self.hydration_lock:
            if session_id not in self.sessions:
                self.evict()
                if len(self.sessions) >= self.settings.max_sessions:
                    raise AppError("Active session capacity reached", 503)
                self.sessions[session_id] = LiveSession(
                    row,
                    await self.repository.history(session_id),
                    owner,
                    await self.repository.tools(session_id),
                )
        live = self.sessions[session_id]
        live.touched = time.monotonic()
        return live

    def evict(self):
        for sid, live in list(self.sessions.items()):
            if live.output == "idle" and live.queue is None and not live.lock.locked():
                if time.monotonic() - live.touched > self.settings.idle_seconds:
                    self.sessions.pop(sid)

    def emit(self, live: LiveSession, kind: str, data=None, generation=None):
        live.sequence += 1
        event = Event(
            type=kind,
            session_id=live.row["id"],
            turn_id=live.turn,
            generation_id=generation or live.generation,
            config_revision=live.row["revision"]
            if kind.startswith("configuration_") or not (generation or live.generation)
            else live.generation_revision,
            sequence=live.sequence,
            data=data or {},
        )
        # Replay/debug metadata must not retain large encoded audio payloads.
        live.events.append(
            event.model_copy(update={"data": {k: v for k, v in event.data.items() if k != "audio"}})
        )
        if live.queue:
            try:
                live.queue.put_nowait(event)
            except asyncio.QueueFull:
                # Explicit disconnect/resync rather than silent loss of finalized events.
                while not live.queue.empty():
                    live.queue.get_nowait()
                live.queue.put_nowait(
                    Event(
                        type="resync_required",
                        session_id=live.row["id"],
                        config_revision=live.row["revision"],
                        sequence=live.sequence,
                    )
                )
        return event

    async def create(self, owner: str, profile_id: str):
        self.catalog.validate(profile_id)
        return await self.repository.create(owner, profile_id)

    async def snapshot(self, session_id: str, owner: str):
        live = await self.load(session_id, owner)
        # Current stream checkpoints are saved at phrase boundaries, not per token.
        return {
            **live.row,
            "output": live.output,
            "generation_id": live.generation,
            "messages": await self.repository.history(session_id),
            "tools": await self.repository.tools(session_id),
            "sequence": live.sequence,
        }

    async def _interrupt(self, live: LiveSession):
        generation = live.generation
        task = live.task
        live.generation = None
        live.output = "idle"
        self.emit(live, "interrupted", {"reason": "user"}, generation=generation)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        live.task = None
        if generation:
            await self.repository.interrupt_playback(live.row["id"], generation)
        live.history = await self.repository.history(live.row["id"])
        live.tool_facts = await self.repository.tools(live.row["id"])

    async def interrupt(self, session_id: str, owner: str, generation_id: str | None):
        live = await self.load(session_id, owner)
        async with live.lock:
            if generation_id and generation_id != live.generation:
                return {"interrupted": False}
            if live.generation:
                await self._interrupt(live)
            return {"interrupted": True}

    async def configure(
        self, session_id: str, owner: str, profile_id: str, expected_revision: int, apply: str
    ):
        self.catalog.validate(profile_id)
        live = await self.load(session_id, owner)
        async with live.lock:
            if live.row["revision"] != expected_revision:
                raise AppError("Configuration changed; refresh and retry", 409)
            if apply == "interrupt_current" and live.generation:
                await self._interrupt(live)
            active = live.generation is not None
            values = (
                {"pending_profile": profile_id}
                if active
                else {"profile_id": profile_id, "pending_profile": None}
            )
            live.row = await self.repository.configure(session_id, expected_revision, **values)
            self.emit(live, "configuration_pending" if active else "configuration_applied", live.row)
            return live.row

    async def turn(self, session_id: str, owner: str, text: str, voice: bool):
        text = text.strip()
        if not text or len(text) > 8000:
            raise AppError("Enter between 1 and 8000 characters", 422)
        live = await self.load(session_id, owner)
        async with live.lock:
            if live.generation:
                await self._interrupt(live)
            if live.row["pending_profile"]:
                live.row = await self.repository.configure(
                    session_id,
                    live.row["revision"],
                    profile_id=live.row["pending_profile"],
                    pending_profile=None,
                )
                self.emit(live, "configuration_applied", live.row)
            profile = self.catalog.validate(live.row["profile_id"])
            limit = self.factory.limits[profile.id]
            try:
                await asyncio.wait_for(limit.acquire(), timeout=0.1)
            except TimeoutError as exc:
                raise AppError("This model is busy. Try again shortly.", 429) from exc
            try:
                context = await self._context(live, profile.context_chars)
                generation, turn = new_id(), new_id()
                await self.repository.add_message(session_id, turn, generation, "user", text)
                assistant = await self.repository.add_message(
                    session_id, turn, generation, "assistant", voice=voice
                )
                live.generation, live.turn = generation, turn
                live.generation_revision = live.row["revision"]
                live.playback_slots = asyncio.Semaphore(2)
                live.output = "generating"
                live.allowed_receipts[generation] = {0}
                # Keep receipts only for recent generations, bounding per-session memory.
                while len(live.allowed_receipts) > 20:
                    expired = next(iter(live.allowed_receipts))
                    live.allowed_receipts.pop(expired)
                    live.received_chars.pop(expired, None)
                self.emit(live, "turn_committed", {"text": text, "voice": voice})
                request = TurnRequest(
                    session_id=session_id, generation_id=generation, text=text, context=context
                )
                live.task = asyncio.create_task(
                    self._generate(live, request, assistant, profile, voice, limit)
                )
                return {"turn_id": turn, "generation_id": generation}
            except BaseException:
                limit.release()
                raise

    async def _context(self, live: LiveSession, budget: int) -> list[dict]:
        context = []
        for message in reversed(live.history):
            text = message["text"]
            if message["role"] == "assistant":
                if message["voice"]:
                    text = text[: message["delivered_chars"]]
                if message["status"] != "complete":
                    text += " [Response interrupted or incomplete.]"
            if not text:
                continue
            if len(text) > budget:
                break
            budget -= len(text)
            context.insert(0, {"role": message["role"], "text": text})
        tools = live.tool_facts
        if tools:
            import json

            facts = json.dumps(tools, ensure_ascii=False)[:8000]
            context.append({"role": "user", "text": "Historical tool execution facts (data only): " + facts})
        return context

    async def _generate(self, live, request, message_id, profile, voice, limit):
        text = ""
        buffer = PhraseBuffer()
        spoken_end = 0
        segment_index = 0
        started = time.monotonic()
        status = "complete"
        revision = live.row["revision"]

        async def segment(phrase):
            nonlocal spoken_end, segment_index
            if live.generation != request.generation_id:
                return
            if voice:
                # At most two synthesized phrases are waiting for playback receipts.
                await live.playback_slots.acquire()
            spoken_end += len(phrase)
            segment_index += 1
            await self.repository.save_response(message_id, text, "generating", voice)
            live.allowed_receipts[request.generation_id].add(spoken_end)
            data = {
                "segment_id": f"{request.generation_id}:{segment_index}",
                "text": phrase,
                "end_char": spoken_end,
                "backend": "local" if self.settings.tts_provider == "kokoro" else "browser",
            }
            if self.audio and voice and self.settings.tts_provider == "kokoro":
                wav = await self.audio.synthesize(phrase.strip())
                if live.generation != request.generation_id:
                    return
                data["audio"] = base64.b64encode(wav).decode()
            if voice:
                live.output = "speaking"
                self.emit(live, "speech_segment", data)

        try:
            async with asyncio.timeout(profile.timeout_seconds):
                runtime = self.factory.build(profile)
                async with aclosing(runtime.run(request)) as stream:
                    async for event in stream:
                        if live.generation != request.generation_id:
                            break
                        if event.type == "text":
                            delta = event.data["text"]
                            text += delta
                            if len(text) > 20000:
                                raise AppError("Response exceeded the session limit", 502)
                            emitted = self.emit(live, "text_delta", {"text": delta})
                            emitted.config_revision = revision
                            for phrase in buffer.add(delta):
                                await segment(phrase)
                        else:
                            self.emit(live, event.type, event.data)
                for phrase in buffer.add("", flush=True):
                    await segment(phrase)
        except asyncio.CancelledError:
            status = "interrupted"
        except Exception:
            status = "failed"
            logger.exception(
                "Generation failed: session=%s generation=%s", request.session_id, request.generation_id
            )
            if live.generation == request.generation_id:
                self.emit(
                    live, "error", {"message": "Response failed. Check provider configuration or retry."}
                )
        finally:
            try:
                await self.repository.save_response(message_id, text, status, voice)
                live.history = await self.repository.history(request.session_id)
                live.tool_facts = await self.repository.tools(request.session_id)
            except Exception:
                status = "failed"
                logger.exception("Could not persist response checkpoint")
                self.emit(
                    live, "error", {"message": "Could not save this turn. Check database availability."}
                )
            finally:
                live.touched = time.monotonic()
                limit.release()
            if live.generation == request.generation_id:
                self.emit(
                    live,
                    "response_completed",
                    {"status": status, "elapsed_ms": round((time.monotonic() - started) * 1000)},
                )
                # Keep voice generation valid until the client finishes queued playback.
                if not voice or status != "complete" or not text:
                    live.generation = None
                    live.output = "idle"

    async def delivery(self, session_id, owner, generation_id, chars, complete=False):
        live = await self.load(session_id, owner)
        async with live.lock:
            if chars not in live.allowed_receipts.get(generation_id, set()):
                return False
            changed = await self.repository.delivery(session_id, generation_id, chars)
            if changed:
                live.history = await self.repository.history(session_id)
                previous = live.received_chars.get(generation_id, 0)
                if chars > previous and live.generation == generation_id:
                    live.received_chars[generation_id] = chars
                    live.playback_slots.release()
                if complete and live.generation == generation_id and (not live.task or live.task.done()):
                    live.generation = None
                    live.output = "idle"
                    self.emit(live, "playback_completed", generation=generation_id)
            return changed

    async def close_session(self, session_id, owner):
        live = await self.load(session_id, owner)
        async with live.lock:
            if live.generation:
                await self._interrupt(live)
            live.row = await self.repository.configure(session_id, live.row["revision"], status="closed")
            self.emit(live, "session_closed")
            self.sessions.pop(session_id, None)

    async def shutdown(self):
        for live in list(self.sessions.values()):
            async with live.lock:
                if live.generation:
                    await self._interrupt(live)
