import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from voice_agent.agents import RuntimeFactory
from voice_agent.controller import SessionController
from voice_agent.domain import AppError
from voice_agent.persistence import Repository
from voice_agent.settings import Catalog, Settings
from voice_agent.speech import LocalAudio

logger = logging.getLogger(__name__)


class CreateSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str | None = None


class TurnBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=8000)
    voice: bool = False


class ConfigurationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    llm_profile: str
    expected_revision: int = Field(ge=1)
    apply: Literal["next_turn", "interrupt_current"] = "next_turn"


class InterruptBody(BaseModel):
    generation_id: str | None = None


class ReceiptBody(BaseModel):
    generation_id: str
    chars: int = Field(ge=0, le=20000)
    complete: bool = False


class AudioInput:
    """Per-connection PCM16/16kHz utterance collector with Pipecat VAD."""

    def __init__(self, controller, live, owner):
        self.controller, self.live, self.owner = controller, live, owner
        self.enabled = False
        self.hands_free = False
        self.detector = None
        self.buffer = bytearray()
        self.pre_roll = deque(maxlen=25)
        self.speaking = False
        self.transcription = None
        self.closed = False
        self.utterances = asyncio.Queue(maxsize=3)

    async def begin(self, hands_free):
        if not self.controller.audio:
            raise AppError("Local audio is not enabled", 422)
        self.enabled = True
        self.closed = False
        self.hands_free = hands_free
        self.buffer.clear()
        self.pre_roll.clear()
        self.speaking = False
        if self.detector:
            await self.detector.cleanup()
        self.detector = self.controller.audio.detector()

    async def frame(self, frame):
        if not self.enabled:
            return
        if len(frame) != 640:
            raise AppError("Audio frames must be 20 ms PCM16 mono at 16000 Hz", 422)
        if not self.hands_free:
            self.buffer.extend(frame)
        else:
            from pipecat.audio.vad.vad_analyzer import VADState

            state = await self.detector.analyze_audio(frame)
            if state == VADState.SPEAKING and not self.speaking:
                self.speaking = True
                self.buffer.extend(b"".join(self.pre_roll))
                await self.controller.interrupt(self.live.row["id"], self.owner, self.live.generation)
                self.controller.emit(self.live, "speech_started")
            if self.speaking:
                self.buffer.extend(frame)
                if state == VADState.QUIET:
                    self.speaking = False
                    self.finish()
            self.pre_roll.append(frame)
        if len(self.buffer) > 16000 * 2 * 30:
            self.buffer.clear()
            self.speaking = False
            self.controller.emit(
                self.live, "error", {"message": "Please keep each spoken turn under 30 seconds."}
            )

    def finish(self):
        if not self.buffer:
            return
        audio = bytes(self.buffer)
        self.buffer.clear()
        try:
            self.utterances.put_nowait(audio)
        except asyncio.QueueFull:
            self.controller.emit(
                self.live,
                "error",
                {"message": "Transcription is catching up. Please wait before repeating your next turn."},
            )
            return
        if self.transcription and not self.transcription.done():
            return

        async def transcribe():
            while not self.utterances.empty():
                utterance = self.utterances.get_nowait()
                self.controller.emit(self.live, "transcribing")
                try:
                    text = await self.controller.audio.transcribe(utterance)
                    if self.closed:
                        return
                    if text:
                        await self.controller.turn(self.live.row["id"], self.owner, text, True)
                    else:
                        self.controller.emit(self.live, "transcript_empty")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Transcription failed")
                    self.controller.emit(
                        self.live, "error", {"message": "Could not transcribe audio. Try again."}
                    )

        self.transcription = asyncio.create_task(transcribe())

    async def close(self):
        self.enabled = False
        self.closed = True
        while not self.utterances.empty():
            self.utterances.get_nowait()
        if self.transcription and not self.transcription.done():
            self.transcription.cancel()
            await asyncio.gather(self.transcription, return_exceptions=True)
        if self.detector:
            await self.detector.cleanup()
            self.detector = None


def create_app(settings: Settings | None = None, runtime_builder=None) -> FastAPI:
    if settings is None:
        from dotenv import load_dotenv

        load_dotenv(override=False)
    settings = settings or Settings()
    catalog = Catalog(settings)
    repository = Repository(settings.database_url)
    factory = RuntimeFactory(catalog, repository)
    if runtime_builder:
        factory.build = runtime_builder
    audio = (
        LocalAudio(settings)
        if settings.stt_provider != "browser" or settings.tts_provider != "browser"
        else None
    )
    controller = SessionController(settings, repository, catalog, factory, audio)
    tickets: dict[str, tuple[str, str, float]] = {}

    @asynccontextmanager
    async def lifespan(app):
        try:
            await repository.check()
            await repository.recover()
            if audio:
                await audio.warmup()
        except Exception:
            await repository.close()
            logger.error("Startup failed. Check the database URL and run `voice-agent migrate` first.")
            raise
        logger.info(
            "Voice Agent started: mode=%s audio=%s database=%s",
            settings.mode,
            settings.audio_backend,
            "PostgreSQL" if settings.mode == "normal" else "SQLite demo",
        )
        try:
            yield
        finally:
            await controller.shutdown()
            await repository.close()

    app = FastAPI(title="Voice Agent", version="0.1.0", lifespan=lifespan)
    app.state.controller = controller
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    def owner(authorization: str | None = Header(default=None)) -> str:
        if settings.api_key:
            token = authorization.removeprefix("Bearer ") if authorization else ""
            if not hmac.compare_digest(token, settings.api_key):
                raise AppError("Valid access key required", 401)
            return hashlib.sha256(token.encode()).hexdigest()
        return "local"

    @app.get("/health")
    async def health():
        await repository.check()
        return {"status": "ok", "mode": settings.mode}

    @app.get("/v1/capabilities")
    async def capabilities(principal=Depends(owner)):
        return {
            "mode": settings.mode,
            "database": "postgresql" if settings.mode == "normal" else "sqlite",
            "profiles": catalog.public(),
            "default_profile": settings.default_profile,
            "audio_backend": settings.audio_backend,
            "stt_provider": settings.stt_provider,
            "tts_provider": settings.tts_provider,
            "protocol": "pcm-websocket-v1",
            "mcp_servers": [{"id": s.id, "tools": s.allowed_tools} for s in catalog.mcp],
        }

    @app.get("/v1/sessions")
    async def sessions(principal=Depends(owner)):
        return await repository.list_sessions(principal)

    @app.post("/v1/sessions", status_code=201)
    async def create(body: CreateSession, principal=Depends(owner)):
        return await controller.create(principal, body.profile_id or settings.default_profile)

    @app.get("/v1/sessions/{session_id}")
    async def snapshot(session_id: str, principal=Depends(owner)):
        return await controller.snapshot(session_id, principal)

    @app.patch("/v1/sessions/{session_id}/configuration")
    async def configure(session_id: str, body: ConfigurationBody, principal=Depends(owner)):
        return await controller.configure(
            session_id, principal, body.llm_profile, body.expected_revision, body.apply
        )

    @app.post("/v1/sessions/{session_id}/turns", status_code=202)
    async def turn(session_id: str, body: TurnBody, principal=Depends(owner)):
        return await controller.turn(session_id, principal, body.text, body.voice)

    @app.post("/v1/sessions/{session_id}/interrupt")
    async def interrupt(session_id: str, body: InterruptBody, principal=Depends(owner)):
        return await controller.interrupt(session_id, principal, body.generation_id)

    @app.post("/v1/sessions/{session_id}/playback")
    async def receipt(session_id: str, body: ReceiptBody, principal=Depends(owner)):
        return {
            "accepted": await controller.delivery(
                session_id, principal, body.generation_id, body.chars, body.complete
            )
        }

    @app.delete("/v1/sessions/{session_id}", status_code=204)
    async def close(session_id: str, principal=Depends(owner)):
        await controller.close_session(session_id, principal)

    @app.post("/v1/sessions/{session_id}/connection")
    async def connection(session_id: str, principal=Depends(owner)):
        await controller.load(session_id, principal)
        for ticket, (_, _, expires) in list(tickets.items()):
            if expires < time.monotonic():
                tickets.pop(ticket)
        if len(tickets) > settings.max_sessions * 4:
            raise AppError("Too many connection requests", 429)
        ticket = secrets.token_urlsafe(32)
        tickets[ticket] = (session_id, principal, time.monotonic() + 30)
        return {
            "ticket": ticket,
            "expires_in": 30,
            "path": f"/v1/sessions/{session_id}/events",
            "audio_backend": settings.audio_backend,
            "stt_provider": settings.stt_provider,
            "tts_provider": settings.tts_provider,
        }

    @app.websocket("/v1/sessions/{session_id}/events")
    async def events(websocket: WebSocket, session_id: str):
        origin = websocket.headers.get("origin")
        if origin and origin not in settings.allowed_origins:
            logger.warning("Session WebSocket rejected: origin=%s is not allowed", origin)
            await websocket.close(code=1008)
            return
        # Ticket authenticates only this session; long-lived API credentials never enter a URL.
        ticket = websocket.query_params.get("ticket", "")
        record = tickets.pop(ticket, None)
        if not record or record[0] != session_id or record[2] < time.monotonic():
            logger.warning("Session WebSocket rejected: connection ticket missing, expired, or mismatched")
            await websocket.close(code=1008)
            return
        principal = record[1]
        try:
            live = await controller.load(session_id, principal)
        except AppError:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue = asyncio.Queue(maxsize=128)
        collector = AudioInput(controller, live, principal)

        async def replace_connection():
            collector.closed = True
            await websocket.close(code=4001, reason="Session opened on another device or tab")

        async with live.lock:
            if live.close_connection:
                await live.close_connection()
            if live.generation:
                await controller._interrupt(live)
            live.queue = queue
            live.close_connection = replace_connection

        async def send():
            while True:
                event = await queue.get()
                await websocket.send_json(event.model_dump())
                if event.type in ("resync_required", "session_closed"):
                    await websocket.close(code=4000, reason="Refresh session state")
                    return

        sender = asyncio.create_task(send())
        controller.emit(live, "connected", {"output": live.output, "profile_id": live.row["profile_id"]})
        try:
            while True:
                packet = await websocket.receive()
                if live.queue is not queue:
                    break
                live.touched = time.monotonic()
                if packet["type"] == "websocket.disconnect":
                    break
                try:
                    if packet.get("bytes") is not None:
                        await collector.frame(packet["bytes"])
                        continue
                    if len(packet.get("text", "")) > 10000:
                        raise AppError("Control message too large", 422)
                    message = json.loads(packet["text"])
                    if not isinstance(message, dict):
                        raise AppError("Control message must be an object", 422)
                    kind = message.get("type")
                    if kind == "playback":
                        receipt = ReceiptBody.model_validate(message["data"])
                        await controller.delivery(
                            session_id, principal, receipt.generation_id, receipt.chars, receipt.complete
                        )
                    elif kind == "capture_start":
                        await collector.begin(bool(message.get("hands_free")))
                    elif kind == "capture_stop":
                        collector.enabled = False
                        collector.finish()
                    elif kind == "capture_cancel":
                        collector.buffer.clear()
                        await collector.close()
                    elif kind == "interrupt":
                        await controller.interrupt(session_id, principal, message.get("generation_id"))
                    elif kind == "ping":
                        controller.emit(live, "pong")
                    else:
                        raise AppError("Unknown control message", 422)
                except (AppError, ValueError, KeyError, TypeError) as exc:
                    controller.emit(live, "error", {"message": str(exc)[:200]})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            await collector.close()
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            async with live.lock:
                # A replaced connection must not clear or interrupt its successor.
                if live.queue is queue:
                    live.queue = None
                    live.close_connection = None
                    if live.generation:
                        await controller._interrupt(live)

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        root = settings.web_dist.resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise AppError("Not found", 404)
        if target.is_file():
            return FileResponse(target)
        index = root / "index.html"
        if path.startswith("v1/") or not index.is_file():
            raise AppError("Not found. Build the web app with npm run build.", 404)
        return FileResponse(index)

    return app
