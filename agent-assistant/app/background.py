"""Single local background worker used by the FastAPI lifespan."""

from __future__ import annotations

import threading

from app.runtime import AssistantRuntime


class BackgroundRunWorker:
    def __init__(self, runtime: AssistantRuntime, *, poll_seconds: float = 0.5) -> None:
        self._runtime = runtime
        self._poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="agent-assistant-worker",
            daemon=True,
        )
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            processed = self._runtime.process_next(raise_errors=False)
            if processed is None:
                self._wake.wait(self._poll_seconds)
                self._wake.clear()

