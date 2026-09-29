import asyncio
import time

from social_crawler.domain.models import CollectionError


class RequestBudget:
    def __init__(self, store, run_id, epoch, config, session=None):
        self.store, self.run_id, self.epoch = store, run_id, epoch
        self.config, self.session = config, session
        run = store.get_run(run_id)
        self.deadline = time.monotonic() + max(0, config.max_seconds - run["elapsed"])
        self.last_request = 0.0
        self.lock = asyncio.Lock()
        self.halted: CollectionError | None = None

    def check(self):
        if self.halted:
            raise self.halted
        if time.monotonic() >= self.deadline:
            raise CollectionError("deadline")
        if self.session:
            self.session.check_unchanged()
        run = self.store.get_run(self.run_id)
        if run["cancel_requested"]:
            raise CollectionError("canceled")
        if run["epoch"] != self.epoch:
            raise CollectionError("stale_worker")

    async def admit(self, operation: str, *, paced: bool = True):
        """Reserve one request; paced=False only for reads of an already paced action."""
        async with self.lock:
            self.check()
            wait = max(0, self.last_request + self.config.min_interval - time.monotonic())
            if not paced:
                wait = 0
            while wait > 0:
                await asyncio.sleep(min(0.1, wait))
                self.check()
                wait = max(0, self.last_request + self.config.min_interval - time.monotonic())
            self.store.reserve_request(self.run_id, self.epoch, operation)
            self.last_request = time.monotonic()

    def halt(self, error):
        if self.halted is None:
            self.halted = error
