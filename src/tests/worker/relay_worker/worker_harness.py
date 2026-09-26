"""The relay worker against the fake node, with timings a hundred times shorter and a clock tests can move.

`RelayWorkerHarness` runs a real RelayWorker (every task, the lock, the database listener) whose
client factory is the fake node's connector. The helpers here configure node_setting for the
fake node, add contacts for simulated devices, and wait for database conditions by polling
instead of sleeping.
"""

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from asgiref.sync import sync_to_async
from django.contrib.auth.hashers import make_password
from django.utils import timezone

from directory.models import Contact, User
from hoptalk_relay.relay_settings import (
    EngineTimingSettings,
    PacingSettings,
    RelaySettings,
    RetryStrategy,
    get_relay_settings,
)
from messaging.models import OutboundPacket
from node.models import NodeSetting, WorkerStatus
from node.node_settings import (
    MANUAL_RADIO_PRESET_TITLE,
    NodeConfiguration,
    NodeSettingKey,
    replace_node_configuration,
)
from tests.manual_clock import ManualClock
from tests.services.messaging.engine_builders import RelayHarness as EngineRelayHarness
from tests.services.messaging.engine_builders import create_device as create_engine_device
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.node_identity import contact_card_uri
from tests.worker.fake_node.simulated_mesh import ReceivedDirectMessage, SimulatedDevice
from tests.worker.fake_node.waiting import wait_until
from worker.connection_supervisor import EXPECTED_FIRMWARE_PROTOCOL_VERSION
from worker.relay_worker import RelayWorker
from worker.worker_timing import WorkerTiming

DATABASE_POLL_INTERVAL_SECONDS = 0.01
WORKER_STOP_TIMEOUT_SECONDS = 15.0
RELAY_MULTI_ACKS = 2

FAST_WORKER_TIMING = WorkerTiming(
    node_command_timeout_seconds=0.5,
    late_reply_grace_seconds=0.02,
    get_next_message_timeout_seconds=0.5,
    contact_listing_activity_seconds=0.5,
    contact_listing_overall_seconds=5.0,
    contact_listing_busy_retry_seconds=0.02,
    factory_reset_reply_seconds=0.1,
    tcp_connect_timeout_seconds=2.0,
    connect_backoff_initial_seconds=0.02,
    connect_backoff_maximum_seconds=0.2,
    stable_connection_seconds=60.0,
    watchdog_interval_seconds=0.05,
    idle_health_check_seconds=30.0,
    client_disconnect_timeout_seconds=1.0,
    single_instance_keepalive_seconds=0.2,
    database_sweep_seconds=0.1,
    drain_poll_seconds=0.5,
    inbound_recording_retry_seconds=0.02,
    incomplete_status_coalescing_seconds=0.05,
    reply_lifetime_seconds=5.0,
    identical_reply_suppression_seconds=0.3,
    minimum_sender_sleep_seconds=0.01,
    maximum_sender_sleep_seconds=0.5,
    table_full_backoff_initial_seconds=0.05,
    table_full_backoff_maximum_seconds=0.2,
    unmatched_acknowledgement_lifetime_seconds=5.0,
    minimum_removal_quiet_wait_seconds=0.5,
    node_restart_quiet_wait_seconds=0.3,
    quiet_poll_seconds=0.01,
    reconciliation_interval_seconds=60.0,
    postponed_removal_retry_seconds=0.3,
    node_command_shutdown_wait_seconds=2.0,
    reboot_disconnect_wait_seconds=2.0,
    reconnect_after_restart_wait_seconds=5.0,
    advert_retry_seconds=0.05,
    status_interval_seconds=0.1,
    maintenance_interval_seconds=60.0,
    task_restart_initial_seconds=0.02,
    task_restart_maximum_seconds=0.2,
    shutdown_step_timeout_seconds=2.0,
)
FAST_RETRY_STRATEGY = RetryStrategy(
    maximum_attempts=3,
    initial_pause_seconds=0.3,
    backoff_multiplier=2.0,
    maximum_pause_seconds=1.0,
    delivered_receipt_delay_seconds=0,
)
FAST_PACING = PacingSettings(
    maximum_packets_awaiting_node_acknowledgement=4,
    minimum_seconds_between_sends=0.01,
    maximum_active_deliveries_per_device=3,
)
FAST_ENGINE_TIMING = EngineTimingSettings(
    minimum_acknowledgement_wait_seconds=0.1,
    maximum_acknowledgement_wait_seconds=1.0,
    unknown_acknowledgement_wait_seconds=0.2,
    missing_parts_round_delay_seconds=0.05,
)


def build_fast_relay_settings(relay_settings: RelaySettings, **overrides: Any) -> RelaySettings:
    return replace(
        relay_settings,
        retry_strategy=overrides.get("retry_strategy", FAST_RETRY_STRATEGY),
        pacing=overrides.get("pacing", FAST_PACING),
        engine_timing=overrides.get("engine_timing", FAST_ENGINE_TIMING),
    )


class AdjustableClock:
    """The system clock, which a test can move forward; sleeps the move covers end at once."""

    def __init__(self) -> None:
        self._offset_seconds = 0.0
        self._moved = asyncio.Event()

    def now(self) -> datetime:
        return timezone.now() + timedelta(seconds=self._offset_seconds)

    def monotonic(self) -> float:
        return time.monotonic() + self._offset_seconds

    async def sleep(self, seconds: float) -> None:
        await self.sleep_until(self.monotonic() + max(seconds, 0.0))

    async def sleep_until(self, monotonic_deadline: float) -> None:
        while True:
            remaining_seconds = monotonic_deadline - self.monotonic()
            if remaining_seconds <= 0:
                return
            moved = self._moved
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(moved.wait(), remaining_seconds)

    def advance(self, *, seconds: float) -> None:
        self._offset_seconds += seconds
        moved, self._moved = self._moved, asyncio.Event()
        moved.set()


class RelayWorkerHarness:
    def __init__(
        self,
        *,
        connector: Any,
        clock: AdjustableClock | None = None,
        timing: WorkerTiming = FAST_WORKER_TIMING,
    ) -> None:
        self.connector = connector
        self.clock = clock or AdjustableClock()
        self.timing = timing
        self.worker = self.build_worker()
        self._run_task: asyncio.Task[None] | None = None

    def build_worker(self) -> RelayWorker:
        return RelayWorker(
            client_factory=self.connector, relay_settings=get_relay_settings(), clock=self.clock, timing=self.timing
        )

    @property
    def runtime_status(self) -> Any:
        return self.worker.worker_state.runtime_status

    def start(self) -> None:
        self._run_task = asyncio.create_task(self.worker.run(), name="relay worker under test")

    async def stop(self) -> None:
        if self._run_task is None:
            return
        self.worker.request_shutdown()
        await asyncio.wait_for(self._run_task, WORKER_STOP_TIMEOUT_SECONDS)
        self._run_task = None

    async def restart(self) -> None:
        """A new worker process: fresh memory, the same database and node."""
        await self.stop()
        self.worker = self.build_worker()
        self.start()

    @property
    def run_task(self) -> asyncio.Task[None] | None:
        return self._run_task

    def forget_run_task(self) -> None:
        """The worker ended by itself; there is nothing left to stop."""
        self._run_task = None

    async def wait_for_relay_mode(self, relay_mode: WorkerStatus.RelayMode, *, timeout_seconds: float = 5.0) -> None:
        await wait_until(
            lambda: self.runtime_status.relay_mode == relay_mode,
            timeout_seconds=timeout_seconds,
            description=f"relay mode {relay_mode} (now {self.runtime_status.relay_mode})",
        )

    async def wait_for_connection_generation(self, generation: int, *, timeout_seconds: float = 5.0) -> None:
        await wait_until(
            lambda: (
                self.runtime_status.connection_generation >= generation
                and self.runtime_status.connection_state == WorkerStatus.ConnectionState.CONNECTED
            ),
            timeout_seconds=timeout_seconds,
            description=f"connection generation {generation}",
        )


async def wait_for_database(
    condition: Callable[[], bool], *, timeout_seconds: float = 5.0, description: str = "the database condition"
) -> None:
    """Poll a synchronous database condition on the worker's database thread."""
    check_condition = sync_to_async(condition, thread_sensitive=True)
    deadline = time.monotonic() + timeout_seconds
    while not await check_condition():
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out after {timeout_seconds} s waiting for {description}")
        await asyncio.sleep(DATABASE_POLL_INTERVAL_SECONDS)


async def in_database[Result](function: Callable[..., Result], *arguments: Any, **keyword_arguments: Any) -> Result:
    return await sync_to_async(function, thread_sensitive=True)(*arguments, **keyword_arguments)


# ----- the fake node as a configured relay node ----------------------------------------------


def make_firmware_a_relay_node(firmware: FakeCompanionFirmware) -> None:
    """The settings a completed setup leaves on the node: manual adding on, no auto-add, two ACKs."""
    preferences = firmware.preferences
    preferences.manual_add_contacts = 1
    preferences.auto_add_configuration = 0
    preferences.auto_add_maximum_hops = 0
    preferences.multi_acknowledgements = RELAY_MULTI_ACKS
    preferences.telemetry_mode_base = 0
    preferences.telemetry_mode_location = 0
    preferences.telemetry_mode_environment = 0
    preferences.advert_location_policy = 0


def build_node_configuration_for(firmware: FakeCompanionFirmware, *, setup_run_id: int = 1) -> NodeConfiguration:
    preferences = firmware.preferences
    build = firmware.build
    return NodeConfiguration(
        node_public_key=firmware.public_key.hex(),
        node_name=preferences.node_name.decode(),
        node_firmware_version=build.firmware_version,
        node_firmware_build=build.build_date,
        node_model=build.manufacturer_name,
        node_protocol_version=EXPECTED_FIRMWARE_PROTOCOL_VERSION,
        node_maximum_contacts=firmware.capacities.maximum_contacts,
        node_contact_card_uri=contact_card_uri(firmware.export_self_card()),
        radio_preset_title=MANUAL_RADIO_PRESET_TITLE,
        radio_frequency_kilohertz=preferences.frequency_kilohertz,
        radio_bandwidth_hertz=preferences.bandwidth_hertz,
        radio_spreading_factor=preferences.spreading_factor,
        radio_coding_rate=preferences.coding_rate,
        radio_transmit_power_dbm=preferences.transmit_power_dbm,
        radio_client_repeat=preferences.client_repeat_enabled,
        routing_path_hash_size=preferences.path_hash_mode + 1,
        messaging_multi_acks=preferences.multi_acknowledgements,
        contacts_manual_add=bool(preferences.manual_add_contacts),
        contacts_auto_add_configuration=preferences.auto_add_configuration,
        contacts_auto_add_maximum_hops=preferences.auto_add_maximum_hops,
        privacy_advert_location_policy=preferences.advert_location_policy,
        privacy_telemetry_modes=preferences.telemetry_modes,
        channels_public_channel_replaced=False,
        setup_completed_at=timezone.now(),
        setup_run_id=setup_run_id,
    )


async def configure_relay_node(firmware: FakeCompanionFirmware) -> NodeConfiguration:
    """The fake node becomes the configured relay node, as after a completed setup."""
    make_firmware_a_relay_node(firmware)
    node_configuration = build_node_configuration_for(firmware)
    await in_database(replace_node_configuration, node_configuration)
    return node_configuration


def point_node_setting_at_another_node(other_public_key: str = "ee" * 32) -> None:
    """node_setting now describes a node other than the attached one."""
    NodeSetting.objects.filter(key=NodeSettingKey.NODE_PUBLIC_KEY.value).update(value=other_public_key)


def create_user(username: str, password: str = "correct horse battery") -> User:
    return User.objects.create(username=username, password_hash=make_password(password), created_at=timezone.now())


def create_contact_for_device(
    device: SimulatedDevice,
    *,
    user: User | None = None,
    node_sync_state: Contact.NodeSyncState = Contact.NodeSyncState.ON_NODE,
) -> Contact:
    now = timezone.now()
    return Contact.objects.create(
        public_key=device.public_key.hex(),
        name=device.name,
        source=Contact.Source.CARD,
        added_at=now,
        user=user,
        linked_at=now if user is not None else None,
        node_sync_state=node_sync_state,
        advert_timestamp=int(now.timestamp()) - 60,
    )


def read_contact(contact_id: int) -> Contact:
    return Contact.objects.get(id=contact_id)


def create_packet_awaiting_acknowledgement_until(deadline_in_seconds: float) -> OutboundPacket:
    """A packet an earlier worker process sent: its firmware ACK may still arrive until the deadline."""
    now = timezone.now()
    return OutboundPacket.objects.create(
        contact_label="an earlier process's packet",
        purpose=OutboundPacket.Purpose.REPLY,
        text="HT1 q bob 1",
        sender_timestamp=int(now.timestamp()) - 5,
        state=OutboundPacket.State.ACKNOWLEDGEMENT_TIMED_OUT,
        route_reset_state=OutboundPacket.RouteResetState.DROPPED_BY_RESTART,
        route=OutboundPacket.Route.DIRECT,
        expected_acknowledgement_code="0badc0de",
        prepared_at=now,
        queued_at=now,
        acknowledgement_deadline_at=now + timedelta(seconds=deadline_in_seconds),
        connection_generation=1,
    )


class DeviceInbox:
    """The direct messages a simulated device's app has read from its node, in arrival order."""

    def __init__(self, device: SimulatedDevice) -> None:
        self.device = device
        self.received_messages: list[ReceivedDirectMessage] = []

    @property
    def texts(self) -> list[str]:
        self.read_new_messages()
        return [received_message.text for received_message in self.received_messages]

    def read_new_messages(self) -> None:
        if self.device.app_can_reach_node:
            self.received_messages.extend(self.device.receive_direct_messages())

    async def wait_for_text(self, expected_text: str, *, timeout_seconds: float = 5.0) -> ReceivedDirectMessage:
        await wait_until(
            lambda: expected_text in self.texts,
            timeout_seconds=timeout_seconds,
            description=f"{self.device.name} to receive {expected_text!r} (so far {self.texts})",
        )
        return next(message for message in self.received_messages if message.text == expected_text)


def create_sending_device(device_number: int, user: User) -> Contact:
    """A device that only sends, through the engine directly: it is not part of the simulated mesh."""
    return create_engine_device(device_number, timezone.now(), user=user)


def accept_messages(
    sender_device: Contact, recipient_username: str, *, message_count: int, part_count: int = 1
) -> None:
    """Messages the sender's device uploaded completely, as the engine accepts them; their deliveries are due now."""
    engine = EngineRelayHarness(ManualClock(current_time=timezone.now()))
    for message_id in range(1, message_count + 1):
        for part_number in range(1, part_count + 1):
            engine.receive(
                sender_device,
                f"HT1 M {recipient_username} {message_id} {part_number}/{part_count} "
                f"part {part_number} of {message_id}",
            )
