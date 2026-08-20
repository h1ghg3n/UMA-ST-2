from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


async def run_blocking_application(operation: Callable[[], T]) -> T:
    """Run one synchronous application port without blocking Discord's event loop."""

    return await asyncio.to_thread(operation)
