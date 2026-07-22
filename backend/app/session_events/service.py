from __future__ import annotations

import asyncio
import copy
import json
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any, AsyncIterator, Awaitable, Callable


@dataclass(frozen=True)
class SessionEvent:
    sequence: int
    session_id: str
    event_type: str
    data: dict[str, Any]
    created_at: str
    byte_size: int
    created_monotonic: float


class SessionEventService:
    """Bounded, active-session-only journal with async SSE polling."""

    def __init__(
        self, max_events: int = 1000, max_bytes: int = 4 * 1024 * 1024,
        retention_seconds: float = 3600,
    ) -> None:
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.retention_seconds = retention_seconds
        self._lock = RLock()
        self._events: deque[SessionEvent] = deque()
        self._sequence = 0
        self._total_bytes = 0
        self._active_session_id: str | None = None

    def activate_session(self, session_id: str, _revision: int = 0) -> None:
        with self._lock:
            if session_id != self._active_session_id:
                self._events.clear()
                self._total_bytes = 0
                self._active_session_id = session_id

    def publish(self, event_type: str, data: dict[str, Any]) -> SessionEvent:
        session_id = str(data.get("sessionId") or self._active_session_id or "")
        encoded = json.dumps(data, default=str, separators=(",", ":")).encode()
        with self._lock:
            if self._active_session_id is None:
                self._active_session_id = session_id
            if session_id != self._active_session_id:
                # Events from a cleaning-up old session are archived, not replayed.
                return SessionEvent(
                    self._sequence, session_id, event_type, {},
                    datetime.now(timezone.utc).isoformat(), 0, time.monotonic(),
                )
            self._sequence += 1
            event = SessionEvent(
                self._sequence, session_id, event_type, copy.deepcopy(data),
                datetime.now(timezone.utc).isoformat(), len(encoded), time.monotonic(),
            )
            self._events.append(event)
            self._total_bytes += event.byte_size
            self._prune_locked()
            return event

    def list(
        self, after: int = 0, session_id: str | None = None,
    ) -> list[SessionEvent]:
        with self._lock:
            self._prune_locked()
            target = session_id or self._active_session_id
            return [
                copy.deepcopy(event) for event in self._events
                if event.sequence > after and event.session_id == target
            ]

    async def stream(
        self, *, session_id: str, after: int = 0,
        is_disconnected: Callable[[], Awaitable[bool]],
    ) -> AsyncIterator[SessionEvent | None]:
        cursor = after
        while self.is_active(session_id) and not await is_disconnected():
            ready = self.list(cursor, session_id)
            if not ready:
                yield None
                await asyncio.sleep(0.25)
                continue
            for event in ready:
                if not self.is_active(session_id):
                    return
                cursor = event.sequence
                yield event

    def is_active(self, session_id: str) -> bool:
        with self._lock:
            return session_id == self._active_session_id

    def _prune_locked(self) -> None:
        cutoff = time.monotonic() - self.retention_seconds
        while self._events and (
            len(self._events) > self.max_events
            or self._events[0].created_monotonic < cutoff
            or (self._total_bytes > self.max_bytes and len(self._events) > 1)
        ):
            self._total_bytes -= self._events.popleft().byte_size
