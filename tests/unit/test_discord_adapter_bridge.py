"""Tests for the shared Discord-to-application worker bridge."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from uma_st2.adapters.discord.common import run_blocking_application


def _run_with_explicit_default_executor[ResultT](operation: Coroutine[Any, Any, ResultT]) -> ResultT:
    """Run the real bridge with deterministic executor ownership in this test."""

    loop = asyncio.new_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    try:
        return loop.run_until_complete(operation)
    finally:
        executor.shutdown()
        loop.close()


def test_blocking_application_bridge_runs_operation_outside_event_loop_thread() -> None:
    event_loop_thread_id = threading.get_ident()

    worker_thread_id = _run_with_explicit_default_executor(run_blocking_application(threading.get_ident))

    assert worker_thread_id != event_loop_thread_id


def test_blocking_application_bridge_propagates_operation_failure() -> None:
    def fail() -> None:
        raise RuntimeError("application failed")

    with pytest.raises(RuntimeError, match="application failed"):
        _run_with_explicit_default_executor(run_blocking_application(fail))
