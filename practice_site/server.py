"""Run the practice site in a background thread (tests and scripts), on a free port."""

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn

from practice_site.app import create_app
from practice_site.config import PracticeSettings


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def running_site(settings: PracticeSettings) -> Iterator[tuple[str, object]]:
    """Yields (base_url, fastapi_app). The server stops when the block ends."""
    app = create_app(settings)
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover - only on a broken machine
            raise RuntimeError("practice site did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}", app
    finally:
        server.should_exit = True
        thread.join(timeout=5)
