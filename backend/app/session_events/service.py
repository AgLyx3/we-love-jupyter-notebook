from __future__ import annotations

import copy
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Condition, RLock
from typing import Any, Iterator


@dataclass(frozen=True)
class SessionEvent:
    sequence: int
    event_type: str
    data: dict[str, Any]
    created_at: str


class SessionEventService:
    def __init__(self, max_events: int = 1000) -> None:
        self._condition = Condition(RLock())
        self._events: deque[SessionEvent] = deque(maxlen=max_events)
        self._sequence = 0

    def publish(self, event_type: str, data: dict[str, Any]) -> SessionEvent:
        with self._condition:
            self._sequence += 1
            event = SessionEvent(self._sequence, event_type, copy.deepcopy(data), datetime.now(timezone.utc).isoformat())
            self._events.append(event)
            self._condition.notify_all()
            return event

    def list(self, after: int = 0) -> list[SessionEvent]:
        with self._condition:
            return [copy.deepcopy(event) for event in self._events if event.sequence > after]

    def stream(self, after: int = 0) -> Iterator[SessionEvent | None]:
        cursor = after
        while True:
            with self._condition:
                ready = [event for event in self._events if event.sequence > cursor]
                if not ready:
                    self._condition.wait(timeout=15)
                    ready = [event for event in self._events if event.sequence > cursor]
            if not ready:
                yield None
                continue
            for event in ready:
                cursor = event.sequence
                yield copy.deepcopy(event)
