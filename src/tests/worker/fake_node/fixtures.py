"""Pytest fixtures for the fake node and the simulated mesh, registered for every test by tests/conftest.py.

Worker tests and end-to-end scenarios both build the relay's node from these, with fast timings,
and every fixture fails the test if the fake itself raised.
"""

from collections.abc import AsyncIterator, Generator
from typing import Any

import pytest

from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.fake_node_transport import FakeNodeConnector
from tests.worker.fake_node.simulated_mesh import SimulatedMesh

# Every random choice of the fake node and the mesh derives from this seed, so a failing run
# can be repeated; a test that wants other draws overrides the fake_node_seed fixture.
FAKE_NODE_SEED = 20260926
RELAY_NODE_NAME = "hoptalk-relay"
# meshcore's own default is 15 s; a lost reply should not stall a test that long.
MESHCORE_COMMAND_TIMEOUT_SECONDS = 1.0


@pytest.fixture
def fake_node_seed() -> int:
    return FAKE_NODE_SEED


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Name the seed in the report of a failed test that used the fake node."""
    report = yield
    fixture_values = getattr(item, "funcargs", {})
    if report.failed and "fake_node_seed" in fixture_values:
        report.sections.append(("fake node seed", str(fixture_values["fake_node_seed"])))
    return report


@pytest.fixture
async def fake_companion_firmware(fake_node_seed: int) -> AsyncIterator[FakeCompanionFirmware]:
    """The relay's node, powered on, with fast timings and no host attached yet."""
    firmware = FakeCompanionFirmware(label="relay", node_name=RELAY_NODE_NAME, seed=fake_node_seed)
    firmware.start()
    yield firmware
    await firmware.stop()
    assert firmware.internal_errors == [], "the fake node itself raised"


@pytest.fixture
async def fake_node_connector(
    fake_companion_firmware: FakeCompanionFirmware, fake_node_seed: int
) -> AsyncIterator[FakeNodeConnector]:
    """The factory the worker calls to get a connected meshcore.MeshCore for the relay's node."""
    connector = FakeNodeConnector(
        fake_companion_firmware, default_timeout=MESHCORE_COMMAND_TIMEOUT_SECONDS, seed=fake_node_seed
    )
    yield connector
    await connector.close()


@pytest.fixture
async def meshcore_client(fake_node_connector: FakeNodeConnector) -> Any:
    """A meshcore.MeshCore connected to the relay's node; the connector disconnects it afterwards."""
    client = await fake_node_connector()
    assert client is not None, "the fake node did not answer the app start"
    return client


@pytest.fixture
async def simulated_mesh(
    fake_companion_firmware: FakeCompanionFirmware, fake_node_seed: int
) -> AsyncIterator[SimulatedMesh]:
    """The mesh around the relay's node; add devices with `simulated_mesh.add_device(name)`."""
    mesh = SimulatedMesh(fake_companion_firmware, seed=fake_node_seed)
    yield mesh
    await mesh.stop()
    assert mesh.internal_errors() == [], f"the simulated mesh (seed {mesh.seed}) itself raised"
