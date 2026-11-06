"""Bounded polling shared by the Redpanda sink services.

A service normally runs until Kubernetes stops it. SIGTERM must end the loop
normally so buffered work is flushed and offsets stay accurate; Python's default
action would skip every ``finally`` block. ``max_idle_seconds`` gives verification
runs a clean exit once the topic is quiet.
"""

from __future__ import annotations

import signal
import time
from typing import Callable, Iterator, Protocol


class PollingConsumer(Protocol):
    """The part of a Kafka consumer the loop uses."""

    def poll(self, timeout: float):
        """Return the next message, or ``None`` when nothing arrived."""


class StopSignal:
    """Record a termination request instead of killing the process."""

    def __init__(self) -> None:
        self.requested = False

    def request(self, *_signal_arguments) -> None:
        """Ask the loop to finish after the current message."""

        self.requested = True

    def install(self) -> "StopSignal":
        """Route SIGTERM and SIGINT to :meth:`request`."""

        signal.signal(signal.SIGTERM, self.request)
        signal.signal(signal.SIGINT, self.request)
        return self


def poll_messages(
    consumer: PollingConsumer,
    stop: StopSignal,
    *,
    max_idle_seconds: float | None = None,
    poll_seconds: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[object | None]:
    """Yield messages until stopped, and ``None`` after each empty poll.

    The ``None`` ticks let a caller flush on a timer while the topic is quiet.

    Args:
        consumer: Source of messages.
        stop: Termination flag.
        max_idle_seconds: End after this long without a message; ``None`` never ends.
        poll_seconds: Timeout of one poll.
        clock: Monotonic time source.

    Raises:
        RuntimeError: The consumer reported a broker error.
    """

    last_message = clock()
    while not stop.requested:
        message = consumer.poll(poll_seconds)
        if message is None:
            if max_idle_seconds is not None and clock() - last_message >= max_idle_seconds:
                return
            yield None
            continue
        if message.error():
            raise RuntimeError(str(message.error()))
        last_message = clock()
        yield message
