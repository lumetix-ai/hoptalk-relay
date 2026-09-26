"""End-to-end scenarios: the real relay worker, the fake node, the simulated mesh and reference HopTalk clients.

Every scenario runs at the speed scenario_settings.py sets, whatever src/.env holds.
"""

from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
from pytest_django import Settings

from hoptalk_relay.relay_settings import RelaySettings
from tests.scenarios.scenario_settings import (
    SCENARIO_CLIENT_TIMING,
    SCENARIO_ENGINE_TIMING,
    SCENARIO_PACING,
    SCENARIO_RETRY_STRATEGY,
    SCENARIO_TIME_FACTOR,
    SCENARIO_WORKER_TIMING,
)
from tests.scenarios.scenario_setup import ClientStarter
from tests.worker.fake_node.fake_node_transport import FakeNodeConnector
from tests.worker.fake_node.simulated_mesh import SimulatedDevice
from tests.worker.relay_worker.worker_harness import RelayWorkerHarness
from tests.worker.simulated_hoptalk_client import SimulatedHopTalkClient
from tests.worker.simulated_hoptalk_client_timing import ClientTiming, ScaledClock


@pytest.fixture(autouse=True)
def use_scenario_relay_settings(settings: Settings) -> None:
    """The scaled production settings, whatever src/.env holds."""
    relay_settings: RelaySettings = settings.RELAY_SETTINGS
    settings.RELAY_SETTINGS = replace(
        relay_settings,
        retry_strategy=SCENARIO_RETRY_STRATEGY,
        pacing=SCENARIO_PACING,
        engine_timing=SCENARIO_ENGINE_TIMING,
    )


@pytest.fixture
async def relay_worker(
    fake_node_connector: FakeNodeConnector, close_sync_to_async_thread_connections: None
) -> AsyncIterator[RelayWorkerHarness]:
    """The relay worker on the fake node, not started yet: the scenario prepares the database and the mesh first."""
    harness = RelayWorkerHarness(connector=fake_node_connector, timing=SCENARIO_WORKER_TIMING)
    yield harness
    await harness.stop()


@pytest.fixture
def client_clock() -> ScaledClock:
    """One wall clock for every client of a scenario, so that MeshCore timestamps never repeat."""
    return ScaledClock(SCENARIO_TIME_FACTOR)


@pytest.fixture
async def start_client(client_clock: ScaledClock) -> AsyncIterator[ClientStarter]:
    """Start a reference client on a device; every client is stopped afterwards and must not have raised."""
    started_clients: list[SimulatedHopTalkClient] = []

    def start_client_on(
        device: SimulatedDevice, *, timing: ClientTiming = SCENARIO_CLIENT_TIMING
    ) -> SimulatedHopTalkClient:
        client = SimulatedHopTalkClient(device, timing=timing, clock=client_clock)
        client.start()
        started_clients.append(client)
        return client

    yield start_client_on
    for client in started_clients:
        await client.stop()
    client_errors = [f"{client!r}: {client.internal_errors!r}" for client in started_clients if client.internal_errors]
    assert client_errors == [], "a simulated client raised internally"
