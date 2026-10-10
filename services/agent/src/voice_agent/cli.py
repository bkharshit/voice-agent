import os
from pathlib import Path
from typing import Annotated

import typer

from voice_agent.settings import Settings

app = typer.Typer(no_args_is_help=True)


@app.command()
def download_models():
    """Download the official Kokoro assets and the default Whisper model into .data/models."""
    import httpx
    from faster_whisper import WhisperModel

    folder = Path(".data/models")
    folder.mkdir(parents=True, exist_ok=True)
    base = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/"
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        for name in ("kokoro-v1.0.onnx", "voices-v1.0.bin"):
            target = folder / name
            if target.exists():
                typer.echo(f"Already present: {target}")
                continue
            typer.echo(f"Downloading {name}…")
            partial = target.with_suffix(target.suffix + ".part")
            with client.stream("GET", base + name) as response:
                response.raise_for_status()
                with partial.open("wb") as output:
                    for chunk in response.iter_bytes():
                        output.write(chunk)
            partial.replace(target)
    model = Settings().whisper_model
    typer.echo(f"Downloading Whisper {model}…")
    WhisperModel(model, device="cpu", compute_type="int8", download_root=str(folder / "whisper"))
    typer.echo("Speech models ready. Start with --audio-backend local.")


def resolve(
    mode: str | None,
    database_url: str | None,
    llm_profile: str | None = None,
    audio_backend: str | None = None,
    host: str | None = None,
    port: int | None = None,
):
    from dotenv import load_dotenv

    load_dotenv(override=False)
    values = {}
    if mode:
        values["mode"] = mode
        if mode == "demo" and not database_url:
            values["database_url"] = "sqlite+aiosqlite:///.data/demo.db"
            if not llm_profile:
                values["default_profile"] = "demo"
    for key, value in [
        ("database_url", database_url),
        ("default_profile", llm_profile),
        ("audio_backend", audio_backend),
        ("host", host),
        ("port", port),
    ]:
        if value is not None:
            values[key] = value
    return Settings(**values)


@app.command()
def migrate(
    mode: Annotated[str | None, typer.Option()] = None,
    database_url: Annotated[str | None, typer.Option()] = None,
):
    """Apply versioned schema migrations before starting the server."""
    from alembic import command
    from alembic.config import Config

    settings = resolve(mode, database_url)
    os.environ["VOICE_MODE"] = settings.mode
    os.environ["VOICE_DATABASE_URL"] = settings.database_url
    if settings.mode == "demo":
        Path(".data").mkdir(exist_ok=True)
    command.upgrade(Config("alembic.ini"), "head")
    typer.echo(f"Schema ready ({settings.mode}).")


@app.command()
def serve(
    mode: Annotated[str | None, typer.Option()] = None,
    database_url: Annotated[str | None, typer.Option()] = None,
    llm_profile: Annotated[str | None, typer.Option()] = None,
    audio_backend: Annotated[str | None, typer.Option()] = None,
    stt_provider: Annotated[str | None, typer.Option()] = None,
    tts_provider: Annotated[str | None, typer.Option()] = None,
    host: Annotated[str | None, typer.Option()] = None,
    port: Annotated[int | None, typer.Option()] = None,
):
    """Run the standalone application (one owning process per deployment)."""
    import uvicorn

    from voice_agent.api import create_app

    settings = resolve(mode, database_url, llm_profile, audio_backend, host, port)
    if stt_provider or tts_provider:
        settings = Settings(
            **{
                **settings.model_dump(),
                **({"stt_provider": stt_provider} if stt_provider else {}),
                **({"tts_provider": tts_provider} if tts_provider else {}),
            }
        )
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        ws_max_size=16384,
        ws_max_queue=32,
        log_level="info",
    )
