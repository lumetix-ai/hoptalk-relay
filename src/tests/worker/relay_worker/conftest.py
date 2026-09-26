from collections.abc import AsyncIterator

import pytest
from pytest_django import Settings

from hoptalk_relay.relay_settings import RelaySettings
from tests.worker.fake_node.fake_node_transport import FakeNodeConnector
from tests.worker.relay_worker.worker_harness import AdjustableClock, RelayWorkerHarness, build_fast_relay_settings


@pytest.fixture(autouse=True)
def use_fast_relay_settings(settings: Settings) -> None:
    """Retry rounds, pacing gaps and acknowledgement floors in milliseconds, whatever src/.env holds."""
    relay_settings: RelaySettings = settings.RELAY_SETTINGS
    settings.RELAY_SETTINGS = build_fast_relay_settings(relay_settings)


@pytest.fixture
def adjustable_clock() -> AdjustableClock:
    return AdjustableClock()


@pytest.fixture
async def relay_worker(
    fake_node_connector: FakeNodeConnector,
    adjustable_clock: AdjustableClock,
    close_sync_to_async_thread_connections: None,
) -> AsyncIterator[RelayWorkerHarness]:
    """A relay worker, not started yet: the test prepares the database and the node first."""
    harness = RelayWorkerHarness(connector=fake_node_connector, clock=adjustable_clock)
    yield harness
    await harness.stop()
