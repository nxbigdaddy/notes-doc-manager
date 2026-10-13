"""Bounded read-retry scheduling; no sleeping, executors, or filesystem access."""

from __future__ import annotations

import heapq
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from notes.domain.models import ReadResult

RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0)
TRANSIENT_READ_CODES = frozenset({
    "changed", "metadata_changed", "sharing_violation", "access_denied", "io_error",
})


def is_retryable(read: ReadResult) -> bool:
    """Status alone is insufficient: cancelled/policy failures must not loop."""
    return read.status in {"pending", "error"} and read.error_code in TRANSIENT_READ_CODES


@dataclass(frozen=True)
class RetryItem:
    document_id: str
    library_id: str
    generation: int
    attempt: int
    token: int = 0


@dataclass
class _State:
    item: RetryItem
    due: float | None
    in_flight: bool = False
    exhausted: bool = False


class RetryQueue:
    """Five retries per document generation, retaining bounded exhausted markers.

    ``record`` accepts ordinary reads and explicit new-event results.
    ``pop_due`` leases due items to the application's existing executor.
    Every leased item must be passed to ``complete`` or ``defer``; this queue
    deliberately never guesses that a slow IO operation has finished.

    A repeated ordinary failure cannot reset a timer or exhaustion. Only a newer
    generation or ``reset=True`` starts a new series. Exhausted markers count
    against capacity to prevent an unbounded stream of failing scans.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_items: int = 10_000):
        if type(max_items) is not int or max_items < 1 or max_items > 10_000:
            raise ValueError("max_items must be between 1 and 10000")
        self._clock = clock
        self._capacity = max_items
        self._lock = threading.RLock()
        self._states: dict[str, _State] = {}
        self._heap: list[tuple[float, int, str]] = []
        self._serial = 0
        self._closed = False
        self._rejected_capacity = 0

    def _now(self, value: float | None) -> float:
        value = float(self._clock() if value is None else value)
        if not math.isfinite(value):
            raise ValueError("time must be finite")
        return value

    @staticmethod
    def _validate(document_id: str, library_id: str, generation: int):
        if not isinstance(document_id, str) or not document_id:
            raise ValueError("document_id is required")
        if not isinstance(library_id, str) or not library_id:
            raise ValueError("library_id is required")
        if type(generation) is not int or generation < 0:
            raise ValueError("generation must be a nonnegative integer")

    def _push(self, state: _State, due: float):
        state.due = due
        state.in_flight = False
        state.exhausted = False
        heapq.heappush(self._heap, (due, state.item.token, state.item.document_id))
        self._compact()

    def _compact(self):
        # Explicit resets/deletes may invalidate heap entries. Keep that storage
        # bounded as well instead of allowing a lazy heap to grow indefinitely.
        if len(self._heap) > 2 * len(self._states) + 64:
            self._heap = [
                (state.due, state.item.token, state.item.document_id)
                for state in self._states.values() if state.due is not None
            ]
            heapq.heapify(self._heap)

    def record(
        self,
        document_id: str,
        library_id: str,
        generation: int,
        read: ReadResult,
        *,
        now: float | None = None,
        reset: bool = False,
    ) -> bool:
        """Schedule an initial transient failure, returning True only if newly queued.

        Successful, cancelled and permanent-failure results clear an existing
        matching/older generation. An old-generation result never affects a
        newer generation. Retry command results should use ``complete`` instead.
        """
        self._validate(document_id, library_id, generation)
        with self._lock:
            if self._closed:
                return False
            state = self._states.get(document_id)
            if state and state.item.generation > generation:
                return False
            if state and state.item.library_id != library_id:
                raise ValueError("document identity cannot change libraries")
            if not is_retryable(read):
                self._states.pop(document_id, None)
                self._compact()
                return False
            if state and state.item.generation == generation and not reset:
                return False
            if state is None and len(self._states) >= self._capacity:
                self._rejected_capacity += 1
                return False
            current = self._now(now)
            self._serial += 1
            state = _State(RetryItem(document_id, library_id, generation, 1, self._serial), None)
            self._states[document_id] = state
            self._push(state, current + RETRY_DELAYS[0])
            return True

    def pop_due(self, now: float | None = None, limit: int = 32) -> tuple[RetryItem, ...]:
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._lock:
            if self._closed:
                return ()
            current = self._now(now)
            result = []
            while self._heap and len(result) < limit:
                due, token, identifier = self._heap[0]
                if due > current:
                    break
                heapq.heappop(self._heap)
                state = self._states.get(identifier)
                if state is None or state.item.token != token or state.due != due:
                    continue
                state.due = None
                state.in_flight = True
                result.append(state.item)
            return tuple(result)

    def _leased(self, item: RetryItem) -> _State | None:
        state = self._states.get(item.document_id)
        return state if state and state.in_flight and state.item == item else None

    def is_current(self, item: RetryItem) -> bool:
        """Check cancellation/reset immediately before a leased actor reads or writes."""
        with self._lock:
            return not self._closed and self._leased(item) is not None

    def complete(self, item: RetryItem, read: ReadResult, now: float | None = None) -> bool:
        """Consume one leased result; stale tokens/generations are ignored."""
        with self._lock:
            if self._closed:
                return False
            state = self._leased(item)
            if state is None:
                return False
            if not is_retryable(read):
                del self._states[item.document_id]
                self._compact()
                return False
            if item.attempt >= len(RETRY_DELAYS):
                state.in_flight = False
                state.exhausted = True
                return False
            current = self._now(now)
            state.item = replace(item, attempt=item.attempt + 1)
            self._push(state, current + RETRY_DELAYS[item.attempt])
            return True

    def defer(self, item: RetryItem, now: float | None = None) -> bool:
        """Submission was busy: retry the same attempt after 0.5s, without spinning."""
        with self._lock:
            if self._closed:
                return False
            state = self._leased(item)
            if state is None:
                return False
            self._push(state, self._now(now) + RETRY_DELAYS[0])
            return True

    def discard(self, document_id: str, generation: int | None = None) -> bool:
        """Remove deleted/successful work; optional generation prevents stale clearing."""
        with self._lock:
            state = self._states.get(document_id)
            if state is None or (generation is not None and state.item.generation != generation):
                return False
            del self._states[document_id]
            self._compact()
            return True

    def discard_library(self, library_id: str) -> int:
        """Clear all retry state, including exhausted markers, for a disabled library."""
        with self._lock:
            identifiers = [key for key, value in self._states.items()
                           if value.item.library_id == library_id]
            for identifier in identifiers:
                del self._states[identifier]
            self._compact()
            return len(identifiers)

    def clear(self):
        """Cancel all pending/in-flight retries without closing future submissions."""
        with self._lock:
            self._states.clear()
            self._heap.clear()

    def close(self):
        with self._lock:
            self._closed = True
            self.clear()

    def health(self) -> dict:
        with self._lock:
            return {
                "tracked": len(self._states),
                "pending": sum(state.due is not None for state in self._states.values()),
                "in_flight": sum(state.in_flight for state in self._states.values()),
                "exhausted": sum(state.exhausted for state in self._states.values()),
                "heap_entries": len(self._heap),
                "capacity": self._capacity,
                "rejected_capacity": self._rejected_capacity,
                "closed": self._closed,
            }
