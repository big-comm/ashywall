"""Run blocking work off the main loop and deliver results on it.

GTK objects must be finalized on the main thread. Callbacks given to the
runner usually reference widgets (a card, a page), so worker threads never
hold the last reference to them: callbacks stay in a table only the main
thread touches, and the work function and its outcome are handed back to
the main thread, which releases them after delivery. Otherwise a worker
dropping the last reference finalizes a widget off the main thread, which
corrupts GTK state and aborts the application.
"""

import gc
import itertools
import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, TypeVar

from gi.repository import GLib

ResultT = TypeVar("ResultT")
log = logging.getLogger(__name__)

# Garbage collection on the main loop (see collect_garbage_on_main).
GC_INTERVAL_SECONDS = 1
FULL_GC_EVERY = 30


def run_on_main(callback: Callable[..., Any], *args: Any) -> None:
    def invoke() -> bool:
        callback(*args)
        return GLib.SOURCE_REMOVE

    GLib.idle_add(invoke)


def collect_garbage_on_main() -> None:
    """Collect reference cycles only from the main loop.

    Python runs the cycle collector in whichever thread happens to allocate,
    so a cycle holding widgets could be finalized by a worker thread.
    """
    gc.disable()
    ticks = itertools.count(1)

    def collect() -> bool:
        gc.collect() if next(ticks) % FULL_GC_EVERY == 0 else gc.collect(0)
        return GLib.SOURCE_CONTINUE

    # Seconds-based, so GLib can batch the wakeup with other timers.
    GLib.timeout_add_seconds(GC_INTERVAL_SECONDS, collect)


class Task:
    """Handle on submitted work; ``cancel()`` (main loop) drops its outcome."""

    __slots__ = ("_future", "_pending", "_runner", "_token")

    def __init__(
        self,
        runner: "BackgroundRunner",
        token: int,
        pending: list[Any],
        future: Future[None],
    ) -> None:
        self._runner = runner
        self._token = token
        self._pending = pending
        self._future = future

    def cancel(self) -> bool:
        """Drop the outcome; True when the work had not started yet."""
        self._runner._callbacks.pop(self._token, None)
        cancelled = self._future.cancel()
        if cancelled:
            self._pending.clear()  # Released here, on the main thread.
        return cancelled

    def wait(self, timeout: float | None = None) -> None:
        """Block until the work ran (tests only: delivery needs the main loop)."""
        self._future.result(timeout)


class BackgroundRunner:
    def __init__(self, max_workers: int, name: str) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=name
        )
        self._tokens = itertools.count()
        # Main thread only: token -> (on_done, on_error).
        self._callbacks: dict[int, tuple[Callable[[Any], None], Any]] = {}

    def submit(
        self,
        work: Callable[[], ResultT],
        on_done: Callable[[ResultT], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> Task:
        """Run ``work`` in a worker; its outcome is delivered on the main loop.

        Call from the main loop. Cancelling the returned task (also from the
        main loop) drops the outcome of work already running, so a late result
        never reaches a page or window that has moved on.
        """
        token = next(self._tokens)
        self._callbacks[token] = (on_done, on_error)
        # Emptied by whichever runs first: the worker taking the work, or the
        # main thread cancelling it before it started.
        pending: list[Callable[[], ResultT]] = [work]
        del work

        def task() -> None:
            try:
                function = pending[0]
            except IndexError:
                return  # Cancelled before it started.
            try:
                box: list[Any] = [True, function()]
            except BaseException as error:  # Delivered (and released) on main.
                box = [False, error]
            del function
            # The worker keeps no reference to the work or its outcome: the
            # main thread releases them (and whatever widgets they reference).
            run_on_main(self._deliver, token, pending, box)

        future = self._executor.submit(task)
        return Task(self, token, pending, future)

    def _deliver(self, token: int, pending: list[Any], box: list[Any]) -> None:
        ok, value = box
        box.clear()
        pending.clear()
        callbacks = self._callbacks.pop(token, None)
        if callbacks is None:
            return  # Cancelled meanwhile.
        on_done, on_error = callbacks
        if ok:
            on_done(value)
        elif on_error is not None and isinstance(value, Exception):
            on_error(value)
        elif isinstance(value, Exception):
            log.error("Background task failed", exc_info=value)
        if not ok and not isinstance(value, Exception):
            raise value  # KeyboardInterrupt and the like, now on the main loop.

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._callbacks.clear()
