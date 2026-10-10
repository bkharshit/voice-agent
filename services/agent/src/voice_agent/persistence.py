"""SQLAlchemy repositories. PostgreSQL is the default; SQLite is explicit demo storage."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from voice_agent.domain import AppError, new_id


def now() -> str:
    return datetime.now(UTC).isoformat()


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    profile_id: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    pending_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


class MessageRow(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_session_position", "session_id", "position", unique=True),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    generation_id: Mapped[str] = mapped_column(String(36), index=True)
    turn_id: Mapped[str] = mapped_column(String(36))
    position: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text, default="")
    delivered_chars: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="complete")
    voice: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class ToolRow(Base):
    __tablename__ = "tool_executions"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    generation_id: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(200))
    arguments: Mapped[dict] = mapped_column(JSON)
    result: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


class Repository:
    def __init__(self, database_url: str):
        self.engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)

    async def check(self):
        async with self.engine.connect() as connection:
            # Query a real table: a healthy server with missing migrations is not ready.
            await connection.execute(select(SessionRow.id).limit(1))

    async def close(self):
        await self.engine.dispose()

    async def recover(self):
        async with self.db.begin() as db:
            await db.execute(
                update(MessageRow).where(MessageRow.status == "generating").values(status="interrupted")
            )
            await db.execute(
                update(ToolRow)
                .where(ToolRow.status == "running")
                .values(status="outcome_unknown", updated_at=now())
            )

    async def create(self, owner: str, profile: str) -> dict:
        row = SessionRow(
            id=new_id(),
            owner=owner,
            profile_id=profile,
            revision=1,
            status="active",
            created_at=now(),
            updated_at=now(),
        )
        async with self.db.begin() as db:
            db.add(row)
        return self.session_dict(row)

    @staticmethod
    def session_dict(row: SessionRow) -> dict:
        return {
            "id": row.id,
            "profile_id": row.profile_id,
            "revision": row.revision,
            "pending_profile": row.pending_profile,
            "status": row.status,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    async def get(self, session_id: str, owner: str) -> dict:
        async with self.db() as db:
            row = await db.get(SessionRow, session_id)
            if not row or row.owner != owner:
                raise AppError("Session not found", 404)
            return self.session_dict(row)

    async def list_sessions(self, owner: str) -> list[dict]:
        async with self.db() as db:
            rows = await db.scalars(
                select(SessionRow)
                .where(SessionRow.owner == owner, SessionRow.status == "active")
                .order_by(SessionRow.updated_at.desc())
                .limit(50)
            )
            return [self.session_dict(row) for row in rows]

    async def configure(self, session_id: str, expected_revision: int, **values) -> dict:
        async with self.db.begin() as db:
            result = await db.execute(
                update(SessionRow)
                .where(SessionRow.id == session_id, SessionRow.revision == expected_revision)
                .values(**values, updated_at=now(), revision=expected_revision + 1)
            )
            if result.rowcount != 1:
                raise AppError("Configuration changed; refresh and retry", 409)
            row = await db.get(SessionRow, session_id)
            return self.session_dict(row)

    async def add_message(
        self,
        session_id: str,
        turn_id: str,
        generation_id: str,
        role: str,
        text: str = "",
        voice: bool = False,
    ) -> str:
        async with self.db.begin() as db:
            position = (
                await db.scalar(
                    select(func.max(MessageRow.position)).where(MessageRow.session_id == session_id)
                )
                or 0
            ) + 1
            row = MessageRow(
                id=new_id(),
                session_id=session_id,
                turn_id=turn_id,
                generation_id=generation_id,
                position=position,
                role=role,
                text=text,
                voice=voice,
                status="generating" if role == "assistant" else "complete",
                delivered_chars=0 if role == "assistant" else len(text),
            )
            db.add(row)
            await db.execute(update(SessionRow).where(SessionRow.id == session_id).values(updated_at=now()))
            return row.id

    async def save_response(self, message_id: str, text: str, status: str, voice: bool):
        async with self.db.begin() as db:
            values = {"text": text, "status": status}
            if not voice:
                values["delivered_chars"] = len(text)
            await db.execute(update(MessageRow).where(MessageRow.id == message_id).values(**values))

    async def delivery(self, session_id: str, generation_id: str, chars: int):
        async with self.db.begin() as db:
            row = await db.scalar(
                select(MessageRow).where(
                    MessageRow.session_id == session_id,
                    MessageRow.generation_id == generation_id,
                    MessageRow.role == "assistant",
                )
            )
            if not row or not row.voice or chars < row.delivered_chars or chars > len(row.text):
                return False
            row.delivered_chars = chars
            return True

    async def interrupt_playback(self, session_id: str, generation_id: str):
        async with self.db.begin() as db:
            await db.execute(
                update(MessageRow)
                .where(
                    MessageRow.session_id == session_id,
                    MessageRow.generation_id == generation_id,
                    MessageRow.role == "assistant",
                    MessageRow.voice.is_(True),
                    MessageRow.delivered_chars < func.length(MessageRow.text),
                )
                .values(status="interrupted")
            )

    async def history(self, session_id: str, limit: int = 100) -> list[dict]:
        async with self.db() as db:
            rows = list(
                await db.scalars(
                    select(MessageRow)
                    .where(MessageRow.session_id == session_id)
                    .order_by(MessageRow.position.desc())
                    .limit(limit)
                )
            )
            return [
                {
                    "id": r.id,
                    "role": r.role,
                    "text": r.text,
                    "generation_id": r.generation_id,
                    "turn_id": r.turn_id,
                    "status": r.status,
                    "voice": r.voice,
                    "delivered_chars": r.delivered_chars,
                    "created_at": r.created_at,
                }
                for r in reversed(rows)
            ]

    async def tool_start(
        self, invocation: str, session_id: str, generation_id: str, name: str, arguments: dict
    ):
        async with self.db.begin() as db:
            db.add(
                ToolRow(
                    id=invocation,
                    session_id=session_id,
                    generation_id=generation_id,
                    name=name,
                    arguments=arguments,
                    status="running",
                )
            )

    async def tool_end(self, invocation: str, status: str, result: Any):
        async with self.db.begin() as db:
            await db.execute(
                update(ToolRow)
                .where(ToolRow.id == invocation)
                .values(status=status, result=result, updated_at=now())
            )

    async def tools(self, session_id: str) -> list[dict]:
        async with self.db() as db:
            rows = await db.scalars(
                select(ToolRow)
                .where(ToolRow.session_id == session_id)
                .order_by(ToolRow.created_at.desc())
                .limit(20)
            )
            return [
                {"id": r.id, "name": r.name, "arguments": r.arguments, "result": r.result, "status": r.status}
                for r in rows
            ]
