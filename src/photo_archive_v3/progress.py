"""Shared cooperative progress, without a task framework."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast


class Cancelled(Exception):
    """Stopped between completed units of work."""


@dataclass
class Progress:
    emit: Callable[[str], None] = print
    cancelled: Callable[[], bool] = lambda: False

    def check_cancelled(self) -> None:
        if self.cancelled():
            raise Cancelled("Cancelled; completed items were kept")

    def tick(self, message: str) -> None:
        self.check_cancelled()
        self.emit(message)


async def background[T](function: Callable[..., T], *args: object) -> T:
    """Run blocking work without the interpreter's default executor shutdown path."""
    import asyncio
    import threading

    loop = asyncio.get_running_loop()
    result: asyncio.Future[T] = loop.create_future()

    def finish(value: T | None, error: BaseException | None) -> None:
        if result.done():
            return
        if error is not None:
            result.set_exception(error)
        else:
            result.set_result(cast(T, value))

    def run() -> None:
        try:
            value = function(*args)
        except BaseException as exc:
            loop.call_soon_threadsafe(finish, None, exc)
        else:
            loop.call_soon_threadsafe(finish, value, None)

    threading.Thread(target=run, daemon=True).start()
    return await result
