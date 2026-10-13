"""Run blocking work off the main loop and deliver results on it."""

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, TypeVar

from gi.repository import GLib

ResultT = TypeVar("ResultT")
log = logging.getLogger(__name__)


def run_on_main(callback: Callable[..., Any], *args: Any) -> None:
    def invoke() -> bool:
        callback(*args)
        return GLib.SOURCE_REMOVE

    GLib.idle_add(invoke)


class BackgroundRunner:
    def __init__(self, max_workers: int, name: str) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=name
        )

    def submit(
        self,
        work: Callable[[], ResultT],
        on_done: Callable[[ResultT], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> Future[None]:
        def task() -> None:
            try:
                result = work()
            except Exception as error:
                if on_error is not None:
                    run_on_main(on_error, error)
                else:
                    log.exception("Background task failed")
                return
            run_on_main(on_done, result)

        return self._executor.submit(task)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
