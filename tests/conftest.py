"""Make the scripts under test importable, and give tests a node to talk to."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from meshcore import MeshCore  # noqa: E402  (after the path fix above)

from fake_node import FakeNode  # noqa: E402


@pytest.fixture
def run_async():
    """Run one coroutine per test, each on its own event loop."""
    return lambda coro: asyncio.run(coro)


async def open_node(**kwargs) -> tuple[FakeNode, MeshCore]:
    """A connected MeshCore talking to a fresh FakeNode."""
    node = FakeNode(**kwargs)
    mc = MeshCore(node, default_timeout=3)
    await mc.connect()
    return node, mc


@pytest.fixture
def node_factory():
    return open_node
