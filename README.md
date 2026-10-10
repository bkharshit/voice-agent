# Voice Agent

A standalone conversational application with a React interface, Google ADK reasoning/tools, selectable LLM profiles, PostgreSQL persistence, and optional self-hosted speech. Other applications can integrate through its REST/control API and TypeScript client.

For a fresh checkout, follow [LOCAL_SETUP.md](LOCAL_SETUP.md) for complete local setup,
separate development servers, Whisper/Vosk options, demo mode and phone testing.

## Run normally with PostgreSQL

Prerequisites: Python 3.12 or 3.13, uv, Node.js 22, and Docker Compose (or an existing PostgreSQL 16+ server). Run commands from the repository root.

```sh
cp .env.example .env
docker compose up -d postgres
uv sync --frozen --python 3.12
npm ci
npm run build
uv run voice-agent migrate
uv run voice-agent serve
```

Open http://localhost:8000. PostgreSQL is mandatory in normal mode. A missing/unreachable database or unapplied migrations fails startup; there is no SQLite fallback. The container database uses a persistent volume. Change the example credentials before any shared deployment.

The default model profile points to local Ollama and expects `qwen3:8b` to be installed. Set `VOICE_DEFAULT_PROFILE` in `.env` or use `--llm-profile` to choose another profile. An existing PostgreSQL server works by setting `VOICE_DATABASE_URL` to its asyncpg URL; Docker is not required in that case.

## Run a demo without PostgreSQL or an LLM account

```sh
uv sync --frozen --python 3.12
npm ci
npm run build
uv run voice-agent migrate --mode demo
uv run voice-agent serve --mode demo
```

Demo mode uses `.data/demo.db` and selects an explicitly labeled deterministic demo responder. It exercises the interface, persistence, playback, and interruption without claiming to be an LLM. You can select a configured real provider in demo mode; SQLite storage stays the same. CLI `--mode demo` overrides the normal-mode URL in `.env` unless you supply a different SQLite URL.

## Change language models

Edit `config/profiles.json` on the server. Profiles declare endpoint, model, secret environment-variable name, tool capability, concurrency, and timeout. They are loaded at startup. Model names are examples; select models your account/server actually supports. No client can submit arbitrary endpoints or server secrets.

Included profiles:

- `ollama-local`: OpenAI-compatible endpoint at `localhost:11434/v1`.
- `ollama-cloud`: direct `ollama.com/v1`, requiring `OLLAMA_API_KEY`.
- `ollama-signed-in`: cloud model through a signed-in local Ollama installation; no key is exposed to this app. Change its model identifier to one installed in your Ollama app.
- `openai`, `gemini`: optional paid provider profiles requiring their respective API keys. They are never fallback providers.
- `demo`: available only when the application is in demo mode.

```sh
uv run voice-agent serve --llm-profile ollama-cloud
uv run voice-agent serve --llm-profile ollama-signed-in
```

The Settings drawer changes a session's profile without restarting the service. By default, a change is queued until the next turn. The immediate option interrupts the current response first. Missing credentials and stale configuration revisions reject the change. Target model existence is ultimately checked by the provider during inference; profile capability declarations should be evaluated against your model before enabling tools.

## Voice options

The default `browser` backend uses browser recognition and speech synthesis, avoiding an additional paid audio API. Recognition availability varies and may send audio to a browser vendor. Browser mode supports explicit start/finish recording; hands-free mode is offered only by the local backend. Text input works in every browser.

For self-hosted voice:

```sh
uv sync --frozen --extra voice --python 3.12
uv run --extra voice voice-agent download-models
uv run --extra voice voice-agent serve --audio-backend local
```

This downloads the official Kokoro model/voices and the configured Whisper model (default `small.en`) into ignored `.data/models` (approximately 800 MB with the default models). Configure model paths/voice in `.env` if needed. Warm local Whisper and Kokoro workers perform inference off the API event loop. Pipecat's Silero VAD supplies hands-free speech start/end signals. The browser sends 20 ms PCM16 mono frames at 16 kHz; replies are phrase-sized WAV segments. Microphone access requires localhost or HTTPS.

The first version uses an authenticated WebSocket for PCM audio and control, instead of the RFC's proposed WebRTC transport. This keeps deployment small and works with the same controller/ADK boundaries. WebRTC, partial STT, semantic endpointing, and adaptive backchannel handling remain future upgrades. Local hands-free endpointing is VAD-based with a configurable 900 ms silence threshold; benchmark it with your microphones and speakers. No call-quality or concurrency guarantees are assumed.

Playback stops locally before server cancellation. Generation IDs reject stale audio. The server permits at most two outstanding speech segments. Receipts checkpoint fully played segments; interrupted partial segments are conservatively considered unheard. Generated text remains visible in the transcript, while later model context contains only acknowledged spoken segments plus persisted tool facts. Changing models never replays tool calls.

## Tools and MCP

Tool-capable profiles expose `calculate` and `current_time` through Google ADK. Every execution records its intent/result in the database and has a deadline. On restart, unresolved tool records become `outcome_unknown`. This release intentionally enables read-only tools only; mutating actions need explicit authorization and idempotency handling before being added.

Add an approved remote MCP server to `mcp_servers` in the profile file:

```json
{
  "id": "workspace",
  "url": "http://127.0.0.1:9000/mcp",
  "allowed_tools": ["search_documents"],
  "read_only": true,
  "token_env": "WORKSPACE_MCP_TOKEN"
}
```

Tools are namespaced (`workspace_search_documents`), allowlisted, and wrapped by the same durable execution/deadline adapter. The administrator is responsible for verifying that allowlisted tools are actually read-only; a configuration flag cannot enforce the behavior of an external server. MCP connections are scoped to an agent run and closed afterward. Discovery occurs before generation, so first-turn MCP latency should be measured; cross-run discovery pooling is not implemented yet.

## Integrate another application

Use `packages/client-sdk/src/index.ts` as the workspace TypeScript client or call `/v1` directly. API documentation is available at `/docs`.

1. Authenticate with the server's access key when configured.
2. Create a session, obtain a short-lived connection ticket, and open its WebSocket.
3. Submit text turns or audio frames; consume typed transcript, speech, tool, and configuration events.
4. Report playback receipts and send generation-specific interrupts.

The API includes session listing/resume/close, configuration revisions, and playback checkpoints. Tickets are single-use and expire after 30 seconds. Events have protocol version, sequence, session/turn/generation IDs, and configuration revision. Reconnect by obtaining a fresh ticket and refreshing the persisted session snapshot. Only one connected client owns a session at a time; an authenticated reconnect takes over and closes the previous socket. Old socket cleanup cannot interrupt its successor. The SDK waits for the server's connected event before declaring the session ready.

Set a strong `VOICE_API_KEY` before binding beyond loopback, and configure exact `VOICE_ALLOWED_ORIGINS`. The current access key represents one workspace identity; separate user authentication/tenant provisioning is not implemented. Deploy one owning application process. Multiple workers would need shared ownership and coordination first. Put remote deployments behind TLS and keep provider credentials server-side.

## Storage and migrations

PostgreSQL stores sessions/configuration, messages/delivery checkpoints, and tool execution records. Bounded active history stays in process memory and is rebuilt after restart. There is no Redis dependency. Context currently retains recent turns and tool facts within a character budget; automatic long-conversation summarization is not implemented.

Use Alembic migrations in both modes. Run them before starting the service. Demo SQLite is a single-process convenience, not a substitute for PostgreSQL tests or production locking guarantees. Changing the database URL does not transfer demo history. Back up PostgreSQL with your hosting provider or PostgreSQL tools and test restoration before deploying shared workloads.

## Development and tests

```sh
# Frontend dev server proxies REST and WebSocket traffic to port 8000.
npm run dev
uv run ruff check services migrations tests
npm run test:web
npm run build

# Use a dedicated migrated PostgreSQL database whose name ends in _test.
export VOICE_DATABASE_URL=postgresql+asyncpg://voice:voice@127.0.0.1:5432/voice_agent_test
uv run voice-agent migrate
export TEST_DATABASE_URL="$VOICE_DATABASE_URL"
# Optional real local MCP integration fixture (separate terminal):
uv run python tests/mcp_fixture.py
export TEST_MCP_URL=http://127.0.0.1:55434/mcp
uv run pytest -q
```

Tests exercise the same session repository against PostgreSQL and migrated SQLite, real ADK tool execution with a deterministic test model, optional real MCP transport, interruption, profile revisions, stale receipts, backpressure, persistence recovery, auth, and browser playback cancellation. Provider inference requires your credentials/account and is verified separately from deterministic tests. GitHub Actions provisions PostgreSQL and the MCP fixture automatically.

With local speech enabled and the service running, `uv run --extra voice python scripts/verify_voice.py` sends synthetic speech through both push-to-talk and hands-free modes and verifies returned WAV segments and playback receipts. This makes two short requests to the configured inference provider. It does not capture your microphone or test acoustic echo cancellation.

The Dockerfile packages the frontend and optional speech dependencies. It requires externally supplied database URL, access key, and local model volume/configuration; it does not run migrations automatically. Build/run commands:

```sh
docker build -t voice-agent .
# Supply a container-reachable PostgreSQL URL in your deployment environment.
docker run --rm --env-file .env voice-agent voice-agent migrate
docker run --rm --env-file .env -p 127.0.0.1:8000:8000 voice-agent
```

For local Docker networking, `localhost` inside the app container refers to the container itself. Use `host.docker.internal` for host PostgreSQL/Ollama on Docker Desktop, or service names on a shared container network.

See [ARCHITECTURE.md](ARCHITECTURE.md) for design boundaries and the original roadmap.

The accuracy defaults use English Whisper `small.en` and beam size 5. Set `VOICE_WHISPER_MODEL`, `VOICE_WHISPER_LANGUAGE` (`en` or `auto`, for example), `VOICE_WHISPER_BEAM_SIZE`, and `VOICE_VAD_STOP_SECONDS` in `.env`. Use a multilingual model when selecting a non-English language. Larger models and wider beams may increase response latency. Capture indicates Listening only after the microphone is ready, flushes its final frame, reports congestion explicitly, and queues up to three completed utterances while STT is busy.

### Independent speech providers (Vosk, Whisper, browser)

Recognition and speech output can be selected independently at server startup:

```bash
uv sync --extra voice --extra vosk
mkdir -p .data/models
curl --fail --location https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip -o .data/models/vosk-small.zip
unzip .data/models/vosk-small.zip -d .data/models
uv run --extra voice --extra vosk voice-agent serve --stt-provider vosk --tts-provider kokoro
```

Stop the existing backend before starting another on the same port, and reload the
browser after changing providers. Keep the tunnel running. Kokoro requires the
existing downloaded assets; use `voice-agent download-models` if absent.

Alternatively set `VOICE_STT_PROVIDER=vosk` and `VOICE_TTS_PROVIDER=kokoro` in `.env`.
Set `VOICE_VOSK_MODEL` to another extracted Vosk model directory to change language
or model size. Whisper uses its existing `VOICE_WHISPER_*` settings. For browser
recognition with Kokoro output use `--stt-provider browser --tts-provider kokoro`;
for Vosk with browser output use `--stt-provider vosk --tts-provider browser`.
Explicit provider settings override the legacy `VOICE_AUDIO_BACKEND` defaults.
These flags affect the whole server and require restart; per-session live switching
is not implemented. Only selected models are loaded. Recognition still runs on
completed utterances, rather than generating incremental Vosk partial transcripts.

Adapters implement `SpeechRecognizer.load/transcribe` in `speech.py` and register
in `STT_PROVIDERS`; transport and agent code do not depend on the recognizer.
Vosk decoder state is isolated per utterance. Missing dependencies/models fail
explicitly instead of silently selecting a different provider.

## Sharing the repository

Commit source, configuration examples and lockfiles. `.env`, `.data`, downloaded
models, local databases, logs, build output and dependencies are ignored. Your
friend should follow [LOCAL_SETUP.md](LOCAL_SETUP.md), create their own `.env`, and
use their own provider credentials. The sample database password is only a
loopback development default.

Before each push, inspect `git status --short` and `git diff --cached`. Never use
`git add -f` to include ignored secrets/data. If a real credential is ever committed,
revoke/rotate it; deleting it in a later commit does not remove it from history.
GitHub source publication does not expose the running application, but the shared
workspace access key is not multiuser authentication. Do not publish your key or
active tunnel configuration. Check third-party model licenses before distributing
model assets; this repository downloads them separately.
