"""The run_relay management command: it waits for the database and its migrations, and stops on SIGTERM."""

import asyncio
import os
import signal
import threading
from typing import Any, ClassVar

import pytest
from django.db import connection

from hoptalk_relay import database_readiness
from hoptalk_relay.relay_settings import get_relay_settings
from worker.management.commands import run_relay


def test_the_worker_waits_until_every_migration_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    unapplied_migrations_by_poll = [["messaging.0002_outbound_packets"], ["messaging.0002_outbound_packets"], []]
    monkeypatch.setattr(database_readiness, "find_unapplied_migrations", lambda: unapplied_migrations_by_poll.pop(0))
    monkeypatch.setattr(connection, "close", lambda: None)

    migrations_are_applied = database_readiness.wait_for_applied_migrations(
        poll_interval_seconds=0.001, stop_requested=threading.Event()
    )

    assert migrations_are_applied
    assert unapplied_migrations_by_poll == []


def test_the_worker_does_not_start_while_migrations_are_pending_and_a_stop_comes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started_workers: list[Any] = []
    monkeypatch.setattr(run_relay, "wait_for_database_connection", lambda **_options: True)
    monkeypatch.setattr(run_relay, "wait_for_applied_migrations", lambda **_options: False)
    monkeypatch.setattr(run_relay, "run_relay_worker", lambda relay_settings: started_workers.append(relay_settings))
    monkeypatch.setattr(run_relay, "request_shutdown_on_termination_signals", lambda shutdown_requested: None)

    run_relay.Command().handle()

    assert started_workers == []


class WorkerWaitingForShutdown:
    """Stands in for the RelayWorker: it runs until asked to shut down."""

    instances: ClassVar[list[WorkerWaitingForShutdown]] = []

    def __init__(self, **_arguments: Any) -> None:
        self.shutdown_requested = asyncio.Event()
        WorkerWaitingForShutdown.instances.append(self)

    def request_shutdown(self) -> None:
        self.shutdown_requested.set()

    async def run(self) -> None:
        await self.shutdown_requested.wait()


async def test_sigterm_asks_the_running_worker_to_shut_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_relay, "RelayWorker", WorkerWaitingForShutdown)
    WorkerWaitingForShutdown.instances.clear()
    worker_run = asyncio.create_task(run_relay.run_relay_worker(get_relay_settings()))
    await asyncio.sleep(0.01)

    os.kill(os.getpid(), signal.SIGTERM)

    await asyncio.wait_for(worker_run, 2)
    assert WorkerWaitingForShutdown.instances[0].shutdown_requested.is_set()
    event_loop = asyncio.get_running_loop()
    for termination_signal in run_relay.TERMINATION_SIGNALS:
        event_loop.remove_signal_handler(termination_signal)
