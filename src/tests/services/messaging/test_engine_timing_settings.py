"""The engine's timing floors and windows come from the relay settings, so a shorter configuration takes effect."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from pytest_django import Settings

from hoptalk_relay.relay_settings import EngineTimingSettings, RelaySettings
from messaging.retry_schedule import calculate_acknowledgement_wait, calculate_missing_parts_round_time
from messaging.route_reset_evidence import needs_flood_arrival_route_reset

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def use_engine_timing(settings: Settings, engine_timing: EngineTimingSettings) -> None:
    relay_settings: RelaySettings = settings.RELAY_SETTINGS
    settings.RELAY_SETTINGS = replace(relay_settings, engine_timing=engine_timing)


def test_the_defaults_are_the_firmware_derived_values() -> None:
    engine_timing = EngineTimingSettings()

    assert engine_timing.minimum_acknowledgement_wait_seconds == 3.0
    assert engine_timing.maximum_acknowledgement_wait_seconds == 60.0
    assert engine_timing.unknown_acknowledgement_wait_seconds == 10.0
    assert engine_timing.missing_parts_round_delay_seconds == 5.0
    assert engine_timing.recent_path_update_seconds == 30.0
    assert engine_timing.flood_arrival_reset_maximum_age_seconds == 60.0
    assert engine_timing.reply_resend_maximum_age_seconds == 60.0


def test_shorter_acknowledgement_floors_and_round_delays_take_effect(settings: Settings) -> None:
    use_engine_timing(
        settings,
        EngineTimingSettings(
            minimum_acknowledgement_wait_seconds=0.1,
            maximum_acknowledgement_wait_seconds=1.0,
            unknown_acknowledgement_wait_seconds=0.2,
            missing_parts_round_delay_seconds=0.05,
        ),
    )

    assert calculate_acknowledgement_wait(50) == timedelta(seconds=0.1)
    assert calculate_acknowledgement_wait(100_000) == timedelta(seconds=1.0)
    assert calculate_acknowledgement_wait(None) == timedelta(seconds=0.2)
    assert calculate_missing_parts_round_time(NOW) == NOW + timedelta(seconds=0.05)


def test_a_shorter_path_update_window_lets_a_flood_arrival_reset_sooner(settings: Settings) -> None:
    use_engine_timing(settings, EngineTimingSettings(recent_path_update_seconds=1.0))

    assert needs_flood_arrival_route_reset(
        arrived_by_flood=True, received_at=NOW, last_path_update_at=NOW - timedelta(seconds=2), now=NOW
    )
