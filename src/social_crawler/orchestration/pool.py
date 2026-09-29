"""In-process worker pool with exclusive access-environment scheduling."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable


@dataclass
class WorkItem:
    work: Callable[[], None]
    resource_keys: frozenset[str]
    on_start: Callable[[int], None]
    on_cancel: Callable[[], None]


class WorkerPool:
    """Run jobs concurrently while never sharing an account/profile resource.

    This is deliberately an in-process scheduler for the single-container stage.
    Its resource-key contract can later be backed by PostgreSQL leases without
    changing collection jobs or platform adapters.
    """

    def __init__(self, workers: int):
        if not 1 <= workers <= 32:
            raise ValueError("workers must be between 1 and 32")
        self.workers = workers
        self.condition = threading.Condition()
        self.pending: deque[WorkItem] = deque()
        self.active_keys: set[str] = set()
        self.closed = False
        self.threads = [
            threading.Thread(
                target=self._run,
                args=(slot,),
                name=f"crawler-worker-{slot}",
                daemon=True,
            )
            for slot in range(1, workers + 1)
        ]
        for thread in self.threads:
            thread.start()

    def submit(
        self,
        work: Callable[[], None],
        *,
        resource_keys: list[str],
        on_start: Callable[[int], None],
        on_cancel: Callable[[], None],
    ):
        item = WorkItem(work, frozenset(resource_keys), on_start, on_cancel)
        with self.condition:
            if self.closed:
                raise RuntimeError("worker pool is closed")
            self.pending.append(item)
            self.condition.notify_all()

    def _eligible_index(self):
        return next(
            (
                index
                for index, item in enumerate(self.pending)
                if item.resource_keys.isdisjoint(self.active_keys)
            ),
            None,
        )

    def _run(self, slot: int):
        while True:
            with self.condition:
                self.condition.wait_for(
                    lambda: self.closed or self._eligible_index() is not None
                )
                index = self._eligible_index()
                if index is None:
                    if self.closed:
                        return
                    continue
                item = self.pending[index]
                del self.pending[index]
                self.active_keys.update(item.resource_keys)
            try:
                item.on_start(slot)
                item.work()
            finally:
                with self.condition:
                    self.active_keys.difference_update(item.resource_keys)
                    self.condition.notify_all()

    def shutdown(self, *, wait_seconds: float = 5):
        with self.condition:
            if self.closed:
                return
            self.closed = True
            pending = list(self.pending)
            self.pending.clear()
            self.condition.notify_all()
        for item in pending:
            item.on_cancel()
        for thread in self.threads:
            thread.join(timeout=wait_seconds)
