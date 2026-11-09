"""Where LangGraph saves graph state between steps, so a paused run survives a restart.

`open_checkpointer(settings)` gives the async SQLite saver on `data/checkpoints.sqlite` with
WAL mode (readers don't block the writer). `durable=False` gives the in-memory saver, for tests
that don't need to survive a process exit.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.config import Settings

# Write a checkpoint after every step and wait for it before going on (crash safety).
DURABILITY = "sync"


def checkpoint_path(data_dir: Path) -> Path:
    return Path(data_dir) / "checkpoints.sqlite"


@asynccontextmanager
async def open_checkpointer(
    settings: Settings, *, durable: bool = True
) -> AsyncIterator[BaseCheckpointSaver]:
    if not durable:
        yield InMemorySaver()
        return
    path = checkpoint_path(settings.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        await saver.conn.execute("PRAGMA journal_mode=WAL")
        await saver.conn.execute("PRAGMA busy_timeout=10000")
        await saver.setup()
        yield saver
