import pytest
from pytest_django import Settings

from tests.manual_clock import ManualClock
from tests.services.messaging.engine_builders import RelayHarness, configure_engine_settings


@pytest.fixture(autouse=True)
def use_default_engine_settings(settings: Settings) -> None:
    configure_engine_settings(settings)


@pytest.fixture
def relay(manual_clock: ManualClock) -> RelayHarness:
    return RelayHarness(manual_clock)
