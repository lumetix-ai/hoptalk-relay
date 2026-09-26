"""The connection to the node: connect, handshake, watch, tear down, back off, and again.

    connecting --(the factory raised or returned nothing)--> back-off 1, 2, 4 ... 30 s --> connecting
    connecting --(a client)--> handshaking --(a step failed)--> teardown --> back-off
    handshaking --(done: the generation grows by one)--> connected
    connected --(link lost, watchdog, reconnect request, link close at shutdown)--> teardown --> connecting

The handshake, one gateway call per step: the device query (which also switches the node to the
message frames that carry the SNR, forgotten at every boot), the node's identity and settings,
its clock, the relay mode; in relay mode running the contact-safety settings are corrected. Last,
the backup of the node's identity is checked and, in relay mode running, taken when it is
missing (worker.node_identity_backup_keeper); a failed backup never fails the handshake. Then
the drain, the reconciliation and the sender are woken. Until the handshake is done no other
task uses the node.

A node relays only once its settings were checked on the current connection: by a handshake in
relay mode running, or by the setup run that read them back. When the mode becomes running
otherwise, as when a setup run that owned the node falls back before its factory reset reached
it, the node is reconnected first, so the handshake checks it.

While connected, a watchdog checks every few seconds that the library's event dispatcher still
runs (it dies silently and for good) and that the link is up, and asks the node for its clock
after a quiet minute. Two failed checks in a row end the connection.

Teardown forgets the packets awaiting a firmware ACK (their ACK may have been lost with the
link), closes the client with a bounded wait, and closes the socket or port directly, since the
library leaves a TCP socket open after its own disconnect heuristic.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from messaging.outbound_packets import mark_packets_awaiting_acknowledgement_dropped
from node.models import WorkerStatus
from node.node_settings import NodeConfiguration, NodeSettingKey, load_node_configuration, update_node_setting
from worker.acknowledgement_tracker import AcknowledgementTracker
from worker.clock import Clock, wait_for_any_event_or_timeout, wait_for_any_event_until
from worker.database_access import run_in_database_thread
from worker.node_clock import NodeClockCheck, check_and_correct_node_clock
from worker.node_connections import NodeClientFactory, close_connection_transport
from worker.node_event_subscriptions import NodeEventSubscriptions
from worker.node_gateway import DeviceInformation, NodeGateway, NodeGatewayError, SelfInformation
from worker.node_identity_backup_keeper import NodeIdentityBackupKeeper
from worker.relay_modes import ConfiguredNode, decide_relay_mode, load_relay_mode_inputs
from worker.settings_drift import (
    ExpectedNodeSettings,
    ReportedNodeSettings,
    SettingDrift,
    find_settings_drift,
    mark_contact_safety_drift_corrected,
    needs_contact_safety_correction,
)
from worker.worker_state import ConnectedNodeDescription, WorkerState
from worker.worker_timing import WorkerTiming

logger = logging.getLogger(__name__)

RelayMode = WorkerStatus.RelayMode
ConnectionState = WorkerStatus.ConnectionState

EXPECTED_FIRMWARE_PROTOCOL_VERSION = 13
SHUTDOWN_REASON = "The relay worker is shutting down."


class HandshakeFailedError(Exception):
    pass


@dataclass(frozen=True, kw_only=True)
class HandshakeOutcome:
    device_information: DeviceInformation
    self_information: SelfInformation
    clock_check: NodeClockCheck
    configured_node: ConfiguredNode
    relay_mode: WorkerStatus.RelayMode
    settings_drift: list[SettingDrift]


class ConnectionSupervisor:
    def __init__(
        self,
        *,
        client_factory: NodeClientFactory,
        gateway: NodeGateway,
        subscriptions: NodeEventSubscriptions,
        acknowledgement_tracker: AcknowledgementTracker,
        identity_backup_keeper: NodeIdentityBackupKeeper,
        worker_state: WorkerState,
        clock: Clock,
        timing: WorkerTiming,
    ) -> None:
        self._client_factory = client_factory
        self._gateway = gateway
        self._subscriptions = subscriptions
        self._acknowledgement_tracker = acknowledgement_tracker
        self._identity_backup_keeper = identity_backup_keeper
        self._worker_state = worker_state
        self._runtime_status = worker_state.runtime_status
        self._signals = worker_state.signals
        self._clock = clock
        self._timing = timing
        self._connection_changed = asyncio.Event()
        self._link_lost = asyncio.Event()
        self._link_lost_reason = ""
        self._reconnect_requested = asyncio.Event()
        self._reconnect_reason = ""
        self._connected_node_public_key: str | None = None
        self._node_settings_checked_on_this_connection = False
        self._reported_configuration_error = ""

    # ----- what other tasks use ------------------------------------------------------------

    @property
    def connection_generation(self) -> int:
        return self._runtime_status.connection_generation

    def request_reconnect(self, reason: str) -> None:
        if not self._reconnect_requested.is_set():
            self._reconnect_reason = reason
            self._reconnect_requested.set()

    def report_disconnection(self, reason: str) -> None:
        self._link_lost_reason = reason
        self._link_lost.set()

    async def wait_for_disconnection(self, connection_generation: int, timeout_seconds: float) -> bool:
        """True once the connection of that generation is gone."""
        return await self._wait_for_connection_change(
            lambda: (
                not (
                    self._runtime_status.connection_state == ConnectionState.CONNECTED
                    and self._runtime_status.connection_generation == connection_generation
                )
            ),
            timeout_seconds,
        )

    async def wait_for_reconnection(self, after_generation: int, timeout_seconds: float) -> bool:
        """True once a handshake after that generation has finished: the node came back."""
        return await self._wait_for_connection_change(
            lambda: (
                self._runtime_status.connection_state == ConnectionState.CONNECTED
                and self._runtime_status.connection_generation > after_generation
            ),
            timeout_seconds,
        )

    async def _wait_for_connection_change(self, condition: Callable[[], bool], timeout_seconds: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while not condition():
            remaining_seconds = deadline - asyncio.get_running_loop().time()
            if remaining_seconds <= 0:
                return False
            try:
                await asyncio.wait_for(self._connection_changed.wait(), remaining_seconds)
            except TimeoutError:
                return condition()
        return True

    def record_node_identity(self, public_key: str) -> None:
        """The node took another identity on this connection (a private key import); later decisions use this key."""
        self._connected_node_public_key = public_key
        connected_node = self._runtime_status.connected_node
        if connected_node is not None and connected_node.public_key != public_key:
            self._runtime_status.connected_node = replace(connected_node, public_key=public_key)
            self._worker_state.report_status_change()

    def confirm_node_settings_verified(self) -> None:
        """The setup run read every value it is about to save back on this connection, as a handshake checks them."""
        self._node_settings_checked_on_this_connection = True

    def withdraw_node_settings_verification(self) -> None:
        """The setup run did not save the values it read back, so they are not the configured ones."""
        self._node_settings_checked_on_this_connection = False

    async def recompute_relay_mode(self) -> WorkerStatus.RelayMode:
        relay_mode_inputs = await run_in_database_thread(load_relay_mode_inputs)
        self._report_configuration_error(relay_mode_inputs.configured_node)
        is_connected = self._runtime_status.connection_state == ConnectionState.CONNECTED
        relay_mode = decide_relay_mode(
            connected_node_public_key=self._connected_node_public_key if is_connected else None,
            configured_node=relay_mode_inputs.configured_node,
            active_setup_run=relay_mode_inputs.active_setup_run,
        )
        if relay_mode == RelayMode.RUNNING and not self._node_settings_checked_on_this_connection:
            self.request_reconnect("the node's settings are checked before it relays")
            return self._runtime_status.relay_mode
        self._publish_relay_mode(relay_mode, relay_mode_inputs.configured_node)
        return relay_mode

    # ----- the loop ------------------------------------------------------------------------

    async def run(self) -> None:
        backoff_seconds = self._timing.connect_backoff_initial_seconds
        while not self._signals.is_shutting_down:
            connected_seconds = await self._connect_once()
            if self._signals.is_shutting_down:
                return
            if connected_seconds >= self._timing.stable_connection_seconds:
                backoff_seconds = self._timing.connect_backoff_initial_seconds
                continue
            await wait_for_any_event_or_timeout(self._clock, [self._signals.shutdown_requested], backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, self._timing.connect_backoff_maximum_seconds)

    async def _connect_once(self) -> float:
        """One attempt; returns how long the connection stayed up after its handshake, 0 when there was none."""
        self._set_connection_state(ConnectionState.CONNECTING)
        try:
            meshcore_client = await self._client_factory()
        except Exception as connect_error:
            self._record_connect_failure(
                f"Connecting to the node over {self._runtime_status.transport_description} failed: "
                f"{describe_exception(connect_error)}"
            )
            return 0.0
        if meshcore_client is None:
            self._record_connect_failure(
                f"The node on {self._runtime_status.transport_description} did not answer the app start."
            )
            return 0.0

        connected_at: float | None = None
        disconnect_reason = SHUTDOWN_REASON
        try:
            await self._perform_handshake(meshcore_client)
            connected_at = self._clock.monotonic()
            disconnect_reason = await self._watch_connection(meshcore_client)
        except HandshakeFailedError as handshake_error:
            disconnect_reason = str(handshake_error)
            self._record_connect_failure(disconnect_reason, is_handshake_failure=True)
        finally:
            await self._tear_down(meshcore_client, disconnect_reason, was_connected=connected_at is not None)
        return 0.0 if connected_at is None else self._clock.monotonic() - connected_at

    def _record_connect_failure(self, error_message: str, *, is_handshake_failure: bool = False) -> None:
        """A node that is unplugged or rebooting is expected now and then; a node that fails its handshake is not."""
        self._runtime_status.consecutive_connect_failures += 1
        self._set_connection_state(ConnectionState.DISCONNECTED)
        log_level = logging.ERROR if is_handshake_failure else logging.WARNING
        logger.log(
            log_level,
            "%s (failed attempts in a row: %d)",
            error_message,
            self._runtime_status.consecutive_connect_failures,
        )
        self._worker_state.record_error(error_message)

    # ----- handshake -----------------------------------------------------------------------

    async def _perform_handshake(self, meshcore_client: Any) -> None:
        self._link_lost.clear()
        self._reconnect_requested.clear()
        self._node_settings_checked_on_this_connection = False
        self._subscriptions.subscribe(meshcore_client)
        self._gateway.attach(meshcore_client)
        self._set_connection_state(ConnectionState.HANDSHAKING)
        try:
            handshake_outcome = await self._run_handshake_steps()
        except NodeGatewayError as handshake_error:
            raise HandshakeFailedError(f"The handshake with the node failed: {handshake_error}") from handshake_error
        self._mark_connected(handshake_outcome)

    async def _run_handshake_steps(self) -> HandshakeOutcome:
        device_information = await self._gateway.query_device()
        if device_information.protocol_version != EXPECTED_FIRMWARE_PROTOCOL_VERSION:
            logger.warning(
                "The node speaks companion protocol %d; the relay was built for %d. Relaying continues.",
                device_information.protocol_version,
                EXPECTED_FIRMWARE_PROTOCOL_VERSION,
            )
        self_information = await self._gateway.read_self_information()
        clock_check = await check_and_correct_node_clock(self._gateway, self._clock, self._timing)

        relay_mode_inputs = await run_in_database_thread(load_relay_mode_inputs)
        self._report_configuration_error(relay_mode_inputs.configured_node)
        relay_mode = decide_relay_mode(
            connected_node_public_key=self_information.public_key,
            configured_node=relay_mode_inputs.configured_node,
            active_setup_run=relay_mode_inputs.active_setup_run,
        )
        settings_drift: list[SettingDrift] = []
        if relay_mode == RelayMode.RUNNING:
            settings_drift = await self._check_configured_settings(device_information, self_information)
        await self._identity_backup_keeper.check_identity_backup(relay_mode)
        return HandshakeOutcome(
            device_information=device_information,
            self_information=self_information,
            clock_check=clock_check,
            configured_node=relay_mode_inputs.configured_node,
            relay_mode=relay_mode,
            settings_drift=settings_drift,
        )

    async def _check_configured_settings(
        self, device_information: DeviceInformation, self_information: SelfInformation
    ) -> list[SettingDrift]:
        """Correct the contact-safety settings at once; report every other difference."""
        node_configuration = await run_in_database_thread(load_node_configuration)
        if node_configuration is None:
            return []
        await run_in_database_thread(refresh_reported_firmware_settings, node_configuration, device_information)

        reported_settings = ReportedNodeSettings(
            self_information=self_information,
            device_information=device_information,
            auto_add_configuration=await self._gateway.read_auto_add_configuration(),
        )
        settings_drift = find_settings_drift(
            ExpectedNodeSettings.from_node_configuration(node_configuration), reported_settings
        )
        if needs_contact_safety_correction(settings_drift):
            await self._correct_contact_safety_settings(node_configuration)
            settings_drift = mark_contact_safety_drift_corrected(settings_drift)
            logger.warning(
                "The node's contact-safety settings had drifted and were corrected: %s",
                describe_settings_drift(settings_drift),
            )
        uncorrected_drift = [drift for drift in settings_drift if not drift.corrected]
        if uncorrected_drift:
            logger.warning(
                "The node's settings differ from the configured ones: %s. Use Re-apply configured settings.",
                describe_settings_drift(uncorrected_drift),
            )
        return settings_drift

    async def _correct_contact_safety_settings(self, node_configuration: NodeConfiguration) -> None:
        await self._gateway.set_other_parameters(
            manual_add_contacts=node_configuration.contacts_manual_add,
            telemetry_modes=node_configuration.privacy_telemetry_modes,
            advert_location_policy=node_configuration.privacy_advert_location_policy,
            multi_acks=node_configuration.messaging_multi_acks,
        )
        await self._gateway.set_auto_add_configuration(
            configuration=node_configuration.contacts_auto_add_configuration,
            maximum_hops=node_configuration.contacts_auto_add_maximum_hops,
        )

    def _mark_connected(self, handshake_outcome: HandshakeOutcome) -> None:
        self_information = handshake_outcome.self_information
        device_information = handshake_outcome.device_information
        self._connected_node_public_key = self_information.public_key
        self._node_settings_checked_on_this_connection = handshake_outcome.relay_mode == RelayMode.RUNNING
        self._runtime_status.connection_generation += 1
        self._runtime_status.connected_since = self._clock.now()
        self._runtime_status.consecutive_connect_failures = 0
        self._runtime_status.node_clock_offset_seconds = handshake_outcome.clock_check.offset_seconds
        self._runtime_status.settings_drift = [drift.to_json() for drift in handshake_outcome.settings_drift]
        self._runtime_status.connected_node = ConnectedNodeDescription(
            public_key=self_information.public_key,
            name=self_information.name,
            firmware_version=device_information.firmware_version,
            model=device_information.model,
            protocol_version=device_information.protocol_version,
            radio_summary=describe_radio(self_information),
        )
        self._set_connection_state(ConnectionState.CONNECTED)
        logger.info(
            "Connected to node %s (%s, %s), generation %d.",
            self_information.name or "without a name",
            self_information.public_key[:12],
            device_information.firmware_version,
            self._runtime_status.connection_generation,
        )
        self._publish_relay_mode(handshake_outcome.relay_mode, handshake_outcome.configured_node)
        if handshake_outcome.relay_mode == RelayMode.RUNNING:
            self._signals.drain_requested.set()
            self._signals.reconcile_requested.set()
            self._signals.sender_wakeup.set()

    # ----- while connected -----------------------------------------------------------------

    async def _watch_connection(self, meshcore_client: Any) -> str:
        """Returns why the connection has to end.

        The database sweep wakes this loop as often as the watchdog is due, so a relay-mode
        wake-up must not move the next check.
        """
        watched_events = [
            self._signals.link_close_requested,
            self._link_lost,
            self._reconnect_requested,
            self._signals.relay_mode_changed,
        ]
        failed_check_count = 0
        next_watchdog_check_at = self._clock.monotonic() + self._timing.watchdog_interval_seconds
        while True:
            await wait_for_any_event_until(self._clock, watched_events, next_watchdog_check_at)
            if self._signals.link_close_requested.is_set():
                return SHUTDOWN_REASON
            if self._link_lost.is_set():
                return f"The link to the node was lost ({self._link_lost_reason})."
            if self._reconnect_requested.is_set():
                return f"Reconnecting to the node: {self._reconnect_reason}."
            if self._signals.relay_mode_changed.is_set():
                self._signals.relay_mode_changed.clear()
                await self._recompute_relay_mode_keeping_the_connection()
            if self._clock.monotonic() < next_watchdog_check_at:
                continue

            passes_watchdog_check = await self._passes_watchdog_check(meshcore_client)
            next_watchdog_check_at = self._clock.monotonic() + self._timing.watchdog_interval_seconds
            if passes_watchdog_check:
                failed_check_count = 0
                continue
            failed_check_count += 1
            if failed_check_count >= self._timing.failed_watchdog_checks_before_reconnect:
                return "The node failed its health checks."

    async def _recompute_relay_mode_keeping_the_connection(self) -> None:
        """A database hiccup must not cost the node connection; the next sweep tries again."""
        try:
            await self.recompute_relay_mode()
        except Exception:
            logger.exception("The relay mode could not be recomputed.")

    async def _passes_watchdog_check(self, meshcore_client: Any) -> bool:
        dispatcher_task = getattr(meshcore_client.dispatcher, "_task", None)
        if dispatcher_task is None or dispatcher_task.done():
            logger.warning("The meshcore event dispatcher has stopped; nothing from the node is read any more.")
            return False
        if not meshcore_client.is_connected:
            return False
        if self._gateway.seconds_since_node_traffic() < self._timing.idle_health_check_seconds:
            return True
        try:
            await self._gateway.read_node_clock()
        except NodeGatewayError as health_check_error:
            logger.warning("The node did not answer a health check: %s", health_check_error)
            return False
        return True

    # ----- teardown ------------------------------------------------------------------------

    async def _tear_down(self, meshcore_client: Any, reason: str, *, was_connected: bool) -> None:
        self._connected_node_public_key = None
        self._gateway.detach()
        self._subscriptions.unsubscribe()
        self._acknowledgement_tracker.forget_packets_awaiting_acknowledgement()
        self._set_connection_state(ConnectionState.DISCONNECTED)
        self._publish_relay_mode(RelayMode.DISCONNECTED, None)
        await self._mark_packets_dropped()
        await self._close_client(meshcore_client)
        if was_connected:
            if reason == SHUTDOWN_REASON:
                logger.info("Disconnected from the node: %s", reason)
            else:
                logger.warning("Disconnected from the node: %s", reason)
                self._worker_state.record_error(reason)

    async def _mark_packets_dropped(self) -> None:
        try:
            dropped_packet_count = await run_in_database_thread(
                mark_packets_awaiting_acknowledgement_dropped, self._clock.now()
            )
        except Exception:
            logger.exception("The packets awaiting a firmware ACK could not be marked dropped.")
            return
        if dropped_packet_count:
            logger.info("%d packets that awaited a firmware ACK were dropped with the link.", dropped_packet_count)

    async def _close_client(self, meshcore_client: Any) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(meshcore_client.disconnect(), self._timing.client_disconnect_timeout_seconds)
        meshcore_client.stop()
        close_connection_transport(meshcore_client.connection_manager.connection)

    # ----- status --------------------------------------------------------------------------

    def _set_connection_state(self, connection_state: WorkerStatus.ConnectionState) -> None:
        if self._runtime_status.connection_state == connection_state:
            return
        self._runtime_status.connection_state = connection_state
        if connection_state != ConnectionState.CONNECTED:
            self._runtime_status.connected_since = None
        self._worker_state.report_status_change()
        self._wake_connection_waiters()

    def _wake_connection_waiters(self) -> None:
        """Every waiter wakes and checks its own condition; later changes use a fresh event."""
        self._connection_changed.set()
        self._connection_changed = asyncio.Event()

    def _publish_relay_mode(self, relay_mode: WorkerStatus.RelayMode, configured_node: ConfiguredNode | None) -> None:
        previous_relay_mode = self._runtime_status.relay_mode
        if previous_relay_mode == relay_mode:
            return
        self._runtime_status.relay_mode = relay_mode
        self._log_relay_mode_change(relay_mode, configured_node)
        self._worker_state.report_status_change()
        self._signals.sender_wakeup.set()
        self._signals.pairing_changed.set()
        if relay_mode == RelayMode.RUNNING:
            self._signals.drain_requested.set()
            self._signals.reconcile_requested.set()

    def _log_relay_mode_change(
        self, relay_mode: WorkerStatus.RelayMode, configured_node: ConfiguredNode | None
    ) -> None:
        if relay_mode != RelayMode.IDENTITY_MISMATCH:
            logger.info("Relay mode: %s.", relay_mode.label.lower())
            return
        attached_node = self._runtime_status.connected_node
        attached_description = (
            f"{attached_node.name or 'unnamed'} ({attached_node.public_key})" if attached_node is not None else "?"
        )
        configured_key = configured_node.public_key if configured_node is not None else "the configured key"
        logger.warning(
            "The attached node %s is not the configured node %s; nothing is relayed until it is.",
            attached_description,
            configured_key or "(unknown)",
        )

    def _report_configuration_error(self, configured_node: ConfiguredNode) -> None:
        configuration_error = configured_node.configuration_error
        if configuration_error and configuration_error != self._reported_configuration_error:
            logger.error(configuration_error)
            self._worker_state.record_error(configuration_error)
        self._reported_configuration_error = configuration_error


def refresh_reported_firmware_settings(
    node_configuration: NodeConfiguration, device_information: DeviceInformation
) -> None:
    """The configured node was updated or re-flashed: node_setting follows what it reports now."""
    reported_values = {
        NodeSettingKey.NODE_FIRMWARE_VERSION: (
            node_configuration.node_firmware_version,
            device_information.firmware_version,
        ),
        NodeSettingKey.NODE_FIRMWARE_BUILD: (node_configuration.node_firmware_build, device_information.firmware_build),
        NodeSettingKey.NODE_MODEL: (node_configuration.node_model, device_information.model),
        NodeSettingKey.NODE_PROTOCOL_VERSION: (
            str(node_configuration.node_protocol_version),
            str(device_information.protocol_version),
        ),
    }
    for key, (configured_value, reported_value) in reported_values.items():
        if reported_value and configured_value != reported_value:
            logger.warning("The node reports %s %s instead of %s.", key.value, reported_value, configured_value)
            update_node_setting(key, reported_value)


def describe_settings_drift(settings_drift: list[SettingDrift]) -> str:
    return ", ".join(
        f"{drift.key.value} is {drift.actual!r}, configured {drift.expected!r}" for drift in settings_drift
    )


def describe_radio(self_information: SelfInformation) -> str:
    """Like "916.575 MHz / SF7 / BW62.5 / CR7"."""
    frequency_megahertz = self_information.radio_frequency_kilohertz / 1000
    bandwidth_kilohertz = self_information.radio_bandwidth_hertz / 1000
    return (
        f"{frequency_megahertz:g} MHz / SF{self_information.radio_spreading_factor} / "
        f"BW{bandwidth_kilohertz:g} / CR{self_information.radio_coding_rate}"
    )


def describe_exception(error: BaseException) -> str:
    """The exception's message, or what a timeout means, since a TimeoutError has none."""
    if isinstance(error, TimeoutError):
        return "no answer in time"
    return str(error) or type(error).__name__
