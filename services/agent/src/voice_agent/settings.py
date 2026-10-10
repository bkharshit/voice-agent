import ipaddress
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from voice_agent.domain import AppError, ModelProfile


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VOICE_", env_file=".env", extra="ignore")
    mode: Literal["normal", "demo"] = "normal"
    database_url: str | None = None
    profiles_path: Path = Path("config/profiles.json")
    default_profile: str = "ollama-local"
    host: str = "127.0.0.1"
    port: int = 8000
    api_key: str = ""
    allowed_origins: list[str] = ["http://localhost:8000", "http://127.0.0.1:8000", "http://localhost:5173"]
    audio_backend: Literal["browser", "local"] = "browser"
    stt_provider: str | None = None
    tts_provider: Literal["browser", "kokoro"] | None = None
    vosk_model: Path = Path(".data/models/vosk-model-small-en-us-0.15")
    whisper_model: str = "small.en"
    whisper_language: str = "en"
    whisper_beam_size: int = Field(default=5, ge=1, le=10)
    vad_stop_seconds: float = Field(default=0.9, ge=0.3, le=3)
    kokoro_model: Path = Path(".data/models/kokoro-v1.0.onnx")
    kokoro_voices: Path = Path(".data/models/voices-v1.0.bin")
    kokoro_voice: str = "af_heart"
    max_sessions: int = Field(default=100, ge=1, le=10000)
    idle_seconds: int = Field(default=1800, ge=60)
    web_dist: Path = Path("apps/web/dist")

    @model_validator(mode="after")
    def validate_mode(self):
        self.stt_provider = self.stt_provider or ("whisper" if self.audio_backend == "local" else "browser")
        self.tts_provider = self.tts_provider or ("kokoro" if self.audio_backend == "local" else "browser")
        if self.mode == "normal":
            if not self.database_url or not self.database_url.startswith("postgresql+asyncpg://"):
                raise ValueError("Normal mode requires VOICE_DATABASE_URL=postgresql+asyncpg://...")
        else:
            if self.database_url is None:
                self.database_url = "sqlite+aiosqlite:///.data/demo.db"
            if not self.database_url.startswith("sqlite+aiosqlite:///"):
                raise ValueError("Demo mode requires a SQLite URL; normal mode uses PostgreSQL")
        try:
            loopback = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            loopback = self.host == "localhost"
        if not loopback and len(self.api_key) < 24:
            raise ValueError("Non-loopback binding requires VOICE_API_KEY with at least 24 characters")
        return self


class McpServer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,30}$")
    url: str
    allowed_tools: list[str] = Field(min_length=1)
    read_only: bool = True
    token_env: str | None = None
    timeout_seconds: float = Field(default=15, ge=1, le=60)


class Catalog:
    def __init__(self, settings: Settings):
        raw = json.loads(settings.profiles_path.read_text())
        self.profiles = {p.id: p for p in map(ModelProfile.model_validate, raw["profiles"])}
        if len(self.profiles) != len(raw["profiles"]):
            raise ValueError("Duplicate model profile IDs")
        self.mcp = [McpServer.model_validate(p) for p in raw.get("mcp_servers", [])]
        if any(not p.read_only for p in self.mcp):
            raise ValueError("This release permits only explicitly allowlisted read-only MCP tools")
        if settings.mode == "demo":
            self.profiles["demo"] = ModelProfile(
                id="demo",
                label="Demo · no LLM",
                provider="demo",
                model="deterministic-demo",
                max_concurrency=4,
            )
        self.get(settings.default_profile)

    def get(self, profile_id: str) -> ModelProfile:
        if profile_id not in self.profiles:
            raise AppError("Unknown model profile", 422)
        return self.profiles[profile_id]

    def validate(self, profile_id: str) -> ModelProfile:
        profile = self.get(profile_id)
        if profile.api_key_env and not os.getenv(profile.api_key_env):
            raise AppError(f"Configure {profile.api_key_env} on the server to use this profile", 422)
        return profile

    def public(self) -> list[dict]:
        return [
            {
                "id": p.id,
                "label": p.label,
                "model": p.model,
                "tool_calling": p.tool_calling,
                "paid": p.paid,
                "available": not p.api_key_env or bool(os.getenv(p.api_key_env)),
            }
            for p in self.profiles.values()
        ]
