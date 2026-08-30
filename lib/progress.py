from __future__ import annotations

from collections import OrderedDict
from threading import Lock
import time
from typing import Any, Callable


ProgressCallback = Callable[..., None]


class OperationProgressRegistry:
    """Thread-safe, bounded progress snapshots for browser-initiated work."""

    def __init__(self, capacity: int = 32):
        self.capacity = max(1, int(capacity))
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = Lock()

    @staticmethod
    def normalize_id(operation_id: str | None) -> str | None:
        value = str(operation_id or "").strip()
        if not value or len(value) > 128:
            return None
        return value

    def begin(
        self,
        operation_id: str | None,
        *,
        operation: str,
        title: str,
        message: str,
        note: str | None = None,
        can_cancel: bool = False,
    ) -> ProgressCallback | None:
        normalized = self.normalize_id(operation_id)
        if normalized is None:
            return None
        now = time.time()
        snapshot = {
            "operation_id": normalized,
            "operation": operation,
            "title": title,
            "status": "running",
            "stage": "starting",
            "message": message,
            "note": note,
            "current": None,
            "total": None,
            "can_cancel": bool(can_cancel),
            "started_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._items[normalized] = snapshot
            self._items.move_to_end(normalized)
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)

        def report(**changes: Any) -> None:
            self.update(normalized, **changes)

        return report

    def update(self, operation_id: str | None, **changes: Any) -> None:
        normalized = self.normalize_id(operation_id)
        if normalized is None:
            return
        with self._lock:
            snapshot = self._items.get(normalized)
            if snapshot is None:
                return
            snapshot.update(changes)
            snapshot["updated_at"] = time.time()
            self._items.move_to_end(normalized)

    def complete(self, operation_id: str | None, message: str = "Done") -> None:
        self.update(
            operation_id,
            status="completed",
            stage="completed",
            message=message,
            can_cancel=False,
        )

    def fail(self, operation_id: str | None, message: str) -> None:
        self.update(
            operation_id,
            status="failed",
            stage="failed",
            message=message,
            can_cancel=False,
        )

    def get(self, operation_id: str | None) -> dict[str, Any] | None:
        normalized = self.normalize_id(operation_id)
        if normalized is None:
            return None
        with self._lock:
            snapshot = self._items.get(normalized)
            return dict(snapshot) if snapshot is not None else None


__all__ = ["OperationProgressRegistry", "ProgressCallback"]
