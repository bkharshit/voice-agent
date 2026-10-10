# Run locally from a fresh checkout

Run all commands from the repository root. Install Git, uv, Node.js 22+, and
Python 3.12 (uv can download Python). PostgreSQL 16+ is required for normal mode;
Docker Compose is the easiest way to run it. Ollama is needed only for Ollama
profiles. Speech models run on the computer hosting the backend, not the phone.

## 1. Install and configure

```bash
git clone <your-repository-url>
cd <repository-directory>
cp .env.example .env
uv sync --frozen --python 3.12
npm ci
npm run build
```

Never commit `.env`. It contains your database URL and provider keys. The default
browser speech mode needs no downloaded speech models.

## 2. Start PostgreSQL and migrate

```bash
docker compose up -d postgres
uv run voice-agent migrate
```

The example `.env` matches Compose: database `voice`, user/password `voice`, port
5432. These are local development defaults, not shared deployment credentials.
If port 5432 is already used, change the Compose host port and `.env` URL together.
Wait for PostgreSQL to be healthy if migrations initially cannot connect.

Without Docker, create a database/user on an existing PostgreSQL server and set:

```env
VOICE_DATABASE_URL=postgresql+asyncpg://YOUR_USER:YOUR_PASSWORD@127.0.0.1:5432/YOUR_DATABASE
```

Then run migrations. Do not copy another developer's database directory or `.env`.

## 3. Choose the LLM

For local Ollama, start Ollama and download the configured model:

```bash
ollama pull qwen3:8b
```

Keep `VOICE_DEFAULT_PROFILE=ollama-local`. This model uses additional local RAM/CPU
beyond the speech backend. Alternatively configure `OLLAMA_API_KEY` and select
`ollama-cloud`, or use `ollama-signed-in` with your own signed-in Ollama installation
and a supported cloud model. Edit `config/profiles.json` to match your models.
The repository contains no account credentials and does not sign you in.

## 4. Start the app

```bash
uv run voice-agent serve
```

Open [localhost:8000](http://localhost:8000). This single process serves the built
frontend and the backend. Connect a session, type a question, then test the mic.
Allow microphone access. Browser recognition support varies by browser and may
use the browser vendor's servers. Text input works without speech recognition.
If `VOICE_API_KEY` is configured, enter that key in the app's Settings.

## 5. Optional Whisper + Kokoro (recommended for recognition accuracy)

Stop the backend with Ctrl+C, then:

```bash
uv sync --frozen --python 3.12 --extra voice
uv run --extra voice voice-agent download-models
uv run --extra voice voice-agent serve --stt-provider whisper --tts-provider kokoro
```

Reload the page after restarting. Model downloads need internet and substantial
disk space; first startup can take time. Whisper defaults to English `small.en`,
CPU INT8 and beam size 5. Change `VOICE_WHISPER_*` in `.env` for other models or
languages. Keep `--extra voice` on subsequent `uv run` commands so uv retains the
optional dependencies. For cached model startup without contacting Hugging Face:

```bash
HF_HUB_OFFLINE=1 uv run --extra voice voice-agent serve --stt-provider whisper --tts-provider kokoro
```

This works only after the Whisper model is downloaded.

## 6. Optional Vosk or mixed providers

```bash
uv sync --frozen --python 3.12 --extra voice --extra vosk
mkdir -p .data/models
curl --fail --location https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip -o .data/models/vosk-small.zip
unzip -n .data/models/vosk-small.zip -d .data/models
uv run --extra voice --extra vosk voice-agent serve --stt-provider vosk --tts-provider kokoro
```

Download Kokoro using step 5 first. macOS uses Vosk 0.3.42 because 0.3.45 lacks a
macOS wheel. Use `VOICE_VOSK_MODEL` for another extracted model directory.
Providers are independent: `--stt-provider browser --tts-provider kokoro` or
`--stt-provider whisper --tts-provider browser` also work. Both browser providers
need no voice extra. Provider changes require a server restart and page reload;
live switching is not implemented. Persistent equivalents are
`VOICE_STT_PROVIDER` and `VOICE_TTS_PROVIDER` in `.env`.

## 7. Frontend/backend development

Terminal 1 (backend with reload, browser speech defaults):

```bash
uv run uvicorn voice_agent.api:create_app --factory --reload --reload-dir services/agent/src --host 127.0.0.1 --port 8000
```

Terminal 2:

```bash
npm run dev
```

Open [localhost:5173](http://localhost:5173). Vite proxies API and WebSocket requests
to port 8000. For local models, set speech providers in `.env` and add
`--extra voice` (and `--extra vosk` if needed) to the backend's `uv run` command.
Do not run two backends on the same port.

## Demo without PostgreSQL or an LLM

```bash
uv run voice-agent migrate --mode demo
uv run voice-agent serve --mode demo --stt-provider browser --tts-provider browser
```

Build the frontend first. This uses ignored `.data/demo.db` and a clearly labeled
deterministic responder, not a real LLM. No provider account is required.

## Phone testing and troubleshooting

Phone microphone access requires HTTPS. To expose the running app with Cloudflare:

1. Generate a workspace key with `uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'`
   and set `VOICE_API_KEY` in `.env`. This key grants access to the whole workspace.
2. Run `cloudflared tunnel --url http://127.0.0.1:8000` in another terminal.
3. Add the exact generated HTTPS origin to the JSON list `VOICE_ALLOWED_ORIGINS`.
4. Restart the backend, open the HTTPS URL on the phone, and enter your workspace key.

Keep backend and tunnel terminals running. Quick-tunnel URLs change when recreated;
update allowed origins accordingly. Share the source repository with your friend,
not your own credentials. Have them create their own local configuration.

- Check readiness: `curl --fail http://127.0.0.1:8000/health`.
- Port in use: `lsof -nP -iTCP:8000 -sTCP:LISTEN`; stop the owning server terminal.
- Database errors: check the URL, PostgreSQL health and migrations.
- Cloudflare UI works but session fails: verify the exact allowed HTTPS origin/key.
- New frontend changes: run `npm run build` for port 8000, or use Vite at port 5173.
- Stop app/tunnel with Ctrl+C; `docker compose stop postgres` preserves the DB volume.
  Avoid `docker compose down -v` unless you intend to delete database data.

See [README.md](README.md) for tests, APIs, MCP tools and deployment limitations.
