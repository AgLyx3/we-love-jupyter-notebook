from __future__ import annotations

from threading import RLock
from uuid import uuid4

from .models import MutationConflict, MutationLease


class MutationCoordinator:
    """Serializes ownership of notebook mutations within the active session."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._active_lease: MutationLease | None = None

    @property
    def active_lease(self) -> MutationLease | None:
        with self._lock:
            return self._active_lease

    def acquire(self, *, operation_type: str, operation_id: str) -> MutationLease:
        with self._lock:
            if self._active_lease is not None:
                raise MutationConflict(
                    active_operation_type=self._active_lease.operation_type,
                    active_operation_id=self._active_lease.operation_id,
                )
            lease = MutationLease(
                token=uuid4().hex,
                operation_type=operation_type,
                operation_id=operation_id,
            )
            self._active_lease = lease
            return lease

    def release(self, lease: MutationLease) -> bool:
        with self._lock:
            if self._active_lease is None or self._active_lease.token != lease.token:
                return False
            self._active_lease = None
            return True
