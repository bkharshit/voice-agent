import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from voice_agent.persistence import Base
from voice_agent.settings import Settings


def migrate(connection):
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def online():
    settings = Settings()
    if settings.mode == "demo":
        from pathlib import Path

        await asyncio.to_thread(Path(".data").mkdir, exist_ok=True)
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as connection:
        await connection.run_sync(migrate)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=Settings().database_url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
