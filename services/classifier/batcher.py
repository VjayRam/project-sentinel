import asyncio
import logging
from dataclasses import dataclass

from config import settings
from metrics import QUEUE_DEPTH

logger = logging.getLogger(__name__)


@dataclass
class _Pending:
    text: str
    future: asyncio.Future


class DynamicBatcher:
    """
    Collects concurrent single-text requests into batches and runs one ORT
    call per batch. After each batch completes, the loop immediately picks up
    whatever is waiting in the queue — empty slots are refilled continuously.

    MAX_BATCH_SIZE:  max texts per ORT call (default 64)
    MAX_WAIT_MS:     how long to wait for a batch to fill before flushing (default 10ms)
    MAX_QUEUE_DEPTH: max pending requests before returning 503 (default 1000)
    """

    def __init__(self, predict_fn) -> None:
        self._predict = predict_fn
        self._queue: asyncio.Queue[_Pending] = asyncio.Queue(maxsize=settings.max_queue_depth)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "DynamicBatcher started | max_batch=%d | max_wait_ms=%.1f | max_queue=%d",
            settings.max_batch_size,
            settings.max_wait_ms,
            settings.max_queue_depth,
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        # Anything still sitting in the queue was never picked up by the loop
        # at all (no batch was formed yet) — fail it the same way so callers
        # don't hang forever waiting on a future nothing will ever resolve.
        while not self._queue.empty():
            pending = self._queue.get_nowait()
            if not pending.future.done():
                pending.future.set_exception(RuntimeError("DynamicBatcher is shutting down"))

    async def submit(self, text: str) -> dict:
        loop = asyncio.get_running_loop()
        pending = _Pending(text=text, future=loop.create_future())
        # Raises asyncio.QueueFull if the queue is at capacity.
        # The route handler catches this and returns 503.
        self._queue.put_nowait(pending)
        QUEUE_DEPTH.set(self._queue.qsize())
        return await pending.future

    async def _loop(self) -> None:
        while True:
            batch: list[_Pending] = []

            # Block until at least one request arrives
            batch.append(await self._queue.get())

            # Drain the queue for up to MAX_WAIT_MS to fill the batch
            deadline = asyncio.get_event_loop().time() + settings.max_wait_ms / 1000
            while len(batch) < settings.max_batch_size:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self._queue.get(), timeout=remaining))
                except asyncio.TimeoutError:
                    break

            QUEUE_DEPTH.set(self._queue.qsize())

            # Run blocking ORT call off the event loop
            loop = asyncio.get_running_loop()
            try:
                results = await loop.run_in_executor(None, self._predict, [p.text for p in batch])
                for pending, result in zip(batch, results):
                    if not pending.future.done():
                        pending.future.set_result(result)
            except asyncio.CancelledError:
                # stop() cancelled us mid-batch — fail whatever we already
                # pulled off the queue instead of leaving those callers to
                # hang forever, then propagate so the task actually stops.
                self._fail_all(batch, RuntimeError("DynamicBatcher is shutting down"))
                raise
            except Exception as exc:
                self._fail_all(batch, exc)

    @staticmethod
    def _fail_all(batch: list["_Pending"], exc: BaseException) -> None:
        for pending in batch:
            if not pending.future.done():
                pending.future.set_exception(exc)
