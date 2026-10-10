import asyncio
import json
import os

import pytest
from sqlalchemy import delete
from voice_agent.agents import RuntimeFactory
from voice_agent.controller import SessionController
from voice_agent.persistence import MessageRow, Repository, SessionRow, ToolRow
from voice_agent.settings import Catalog, Settings


@pytest.fixture(params=["postgres", "demo"])
async def system(request, tmp_path):
    if request.param == "postgres":
        url = os.getenv("TEST_DATABASE_URL")
        if not url:
            pytest.skip("Set TEST_DATABASE_URL to a migrated PostgreSQL database ending in _test")
        if not url.split("?")[0].endswith("_test"):
            pytest.fail("Refusing to clear a database whose name does not end in _test")
        mode = "normal"
    else:
        url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
        mode = "demo"
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "id": "first",
                        "label": "First",
                        "provider": "ollama",
                        "model": "fake",
                        "tool_calling": True,
                    },
                    {"id": "second", "label": "Second", "provider": "ollama", "model": "fake-2"},
                    {
                        "id": "missing-key",
                        "label": "Missing",
                        "provider": "openai",
                        "model": "fake",
                        "api_key_env": "VOICE_TEST_MISSING_KEY",
                    },
                ]
            }
        )
    )
    settings = Settings(
        _env_file=None, mode=mode, database_url=url, profiles_path=path, default_profile="first"
    )
    repo = Repository(url)
    if mode == "demo":
        # Demo exercises the real migration, not metadata.create_all.
        import subprocess
        import sys

        env = dict(os.environ, VOICE_MODE="demo", VOICE_DATABASE_URL=url)
        await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env=env,
            check=True,
            capture_output=True,
        )
    await repo.check()
    async with repo.db.begin() as db:
        for table in (ToolRow, MessageRow, SessionRow):
            await db.execute(delete(table))
    catalog = Catalog(settings)
    factory = RuntimeFactory(catalog, repo)
    controller = SessionController(settings, repo, catalog, factory)
    yield settings, repo, catalog, factory, controller
    await controller.shutdown()
    await repo.close()
