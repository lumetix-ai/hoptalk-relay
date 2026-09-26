import asyncio
import logging
import signal
import threading
import warnings
from types import FrameType
from typing import Any

from django.core.management.base import BaseCommand
from django.db import connections

from hoptalk_relay.database_readiness import wait_for_applied_migrations, wait_for_database_connection
from hoptalk_relay.relay_settings import RelaySettings, get_relay_settings
from worker.node_connections import build_node_client_factory
from worker.relay_worker import RelayWorker
from worker.worker_timing import WorkerTiming

logger = logging.getLogger("worker.run_relay")

DATABASE_POLL_INTERVAL_SECONDS = 5.0
TERMINATION_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class Command(BaseCommand):
    help = "Run the relay worker, the only process that talks to the MeshCore node."

    def handle(self, *arguments: Any, **options: Any) -> None:
        ignore_meshcore_coroutine_function_deprecation()
        relay_settings = get_relay_settings()
        shutdown_requested = threading.Event()
        request_shutdown_on_termination_signals(shutdown_requested)

        logger.info("Relay worker starting, node connection %s", relay_settings.node_connection.describe())

        database_is_ready = wait_for_database_connection(
            timeout_seconds=None,
            poll_interval_seconds=DATABASE_POLL_INTERVAL_SECONDS,
            stop_requested=shutdown_requested,
        ) and wait_for_applied_migrations(
            poll_interval_seconds=DATABASE_POLL_INTERVAL_SECONDS,
            stop_requested=shutdown_requested,
        )
        # The worker's own ORM work runs on its executor thread with connections of its own.
        connections.close_all()

        if database_is_ready:
            asyncio.run(run_relay_worker(relay_settings))
        else:
            logger.info("Relay worker stopped before the database was ready")


async def run_relay_worker(relay_settings: RelaySettings) -> None:
    timing = WorkerTiming()
    relay_worker = RelayWorker(
        client_factory=build_node_client_factory(relay_settings.node_connection, timing),
        relay_settings=relay_settings,
        timing=timing,
    )
    event_loop = asyncio.get_running_loop()
    for termination_signal in TERMINATION_SIGNALS:
        event_loop.add_signal_handler(termination_signal, request_worker_shutdown, relay_worker, termination_signal)
    await relay_worker.run()


def request_worker_shutdown(relay_worker: RelayWorker, termination_signal: signal.Signals) -> None:
    logger.info("Received %s, shutting down", termination_signal.name)
    relay_worker.request_shutdown()


def ignore_meshcore_coroutine_function_deprecation() -> None:
    # meshcore 2.3.14 calls asyncio.iscoroutinefunction(), deprecated in Python 3.14 and
    # removed in 3.16; the filter is as narrow as the one in pyproject.toml.
    warnings.filterwarnings(
        "ignore",
        message="'asyncio.iscoroutinefunction' is deprecated",
        category=DeprecationWarning,
        module="meshcore",
    )


def request_shutdown_on_termination_signals(shutdown_requested: threading.Event) -> None:
    """While the worker waits for the database, before its event loop runs."""

    def handle_termination_signal(signal_number: int, _frame: FrameType | None) -> None:
        logger.info("Received %s, shutting down", signal.Signals(signal_number).name)
        shutdown_requested.set()

    for termination_signal in TERMINATION_SIGNALS:
        signal.signal(termination_signal, handle_termination_signal)
