"""Shared bootstrap state, kept separate to avoid circular imports."""

from __future__ import annotations

import asyncio

_task: asyncio.Task[None] | None = None
_done = False


def set_task(task: asyncio.Task[None]) -> None:
    global _task
    _task = task


def mark_done() -> None:
    global _done
    _done = True


def is_bootstrapping() -> bool:
    return _task is not None and not _task.done()


def is_done() -> bool:
    return _done


def get_task() -> asyncio.Task[None] | None:
    return _task
