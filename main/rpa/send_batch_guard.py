"""Batch-send control helpers for the RPA client."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any


def send_until_blocked(
    tasks: Iterable[dict],
    send_one: Callable[[dict], Any],
    is_blocked: Callable[[str], bool],
    sleep_seconds: float = 0.0,
    sleep_func: Callable[[float], Any] | None = None,
) -> int:
    sent_count = 0
    for task in tasks:
        if is_blocked("before_task"):
            break
        send_one(task)
        sent_count += 1
        if is_blocked("after_task"):
            break
        if sleep_seconds and sleep_func:
            sleep_func(sleep_seconds)
    return sent_count
