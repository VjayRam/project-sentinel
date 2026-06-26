import asyncio
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_MAX_BATCH_SIZE = int(os.environ.get("MAX_BATCH_SIZE", "64"))
_MAX_WAIT_MS = float(os.environ.get("MAX_WAIT_MS", "10"))


@dataclass
class _Pending:
    text: str
    future: asyncio.Future = field(default_factory=asyncio.Future)


class DynamicBatcher:
    """
    Collects concurrent single-text requests into batches and runs one ORT
    call per batch. After each batch completes, the loop immediately picks up
    whatever is waiting in the queue — empty slots are refilled continuously.

    MAX_BATCH_SIZE: max texts per ORT call (env var, default 64)
    MAX_WAIT_MS:    how long to wait for a batch to fill before flushing (env var, default 10ms)
    """

    def __init__(self, predict_fn) -> None:
        self._predict = predict_fn
        self._queue: asyncio.Queue[_Pending] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "DynamicBatcher started | max_batch=%d | max_wait_ms=%.1f",
            _MAX_BATCH_SIZE,
            _MAX_WAIT_MS,
        )

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def submit(self, text: str) -> dict:
        loop = asyncio.get_running_loop()
        pending = _Pending(text=text, future=loop.create_future())
        await self._queue.put(pending)
        return await pending.future

    async def _loop(self) -> None:
        while True:
            batch: list[_Pending] = []

            # Block until at least one request arrives
            batch.append(await self._queue.get())

            # Drain the queue for up to MAX_WAIT_MS to fill the batch
            deadline = asyncio.get_event_loop().time() + _MAX_WAIT_MS / 1000
            while len(batch) < _MAX_BATCH_SIZE:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self._queue.get(), timeout=remaining))
                except asyncio.TimeoutError:
                    break

            # Run blocking ORT call off the event loop
            loop = asyncio.get_running_loop()
            try:
                results = await loop.run_in_executor(
                    None, self._predict, [p.text for p in batch]
                )
                for pending, result in zip(batch, results):
                    if not pending.future.done():
                        pending.future.set_result(result)
            except Exception as exc:
                for pending in batch:
                    if not pending.future.done():
                        pending.future.set_exception(exc)
