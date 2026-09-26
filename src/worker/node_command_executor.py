"""Claiming and running the panel's node commands, one at a time.

Before every claim, commands past their expiry become expired (moving a setup run back) and the
relay mode is recomputed; a command of a kind the mode does not allow fails with a reason the
operator can read. While the node is disconnected nothing is claimed: commands wait and expire.
Every step is written to the command's progress as it happens, and traffic keeps flowing between
the command's node calls unless the command pauses sending.

At shutdown no new command is claimed, and the running one gets a few seconds to finish; one
still running when the process ends is marked interrupted at the next start. A command whose
end could not be written, because the task failed, would stay running and block every later
claim, so the restarted task marks it interrupted.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from node.contact_cards import InvalidContactCardError
from node.models import NodeCommand
from node.node_commands import claim_next_node_command, expire_pending_node_commands, finish_node_command
from node.node_settings import NodeSettingKey, load_node_configuration, update_node_setting
from node.pairing_sessions import stop_pairing_session
from worker.clock import Clock, wait_for_any_event_or_timeout
from worker.command_progress import NodeCommandFailedError, NodeCommandProgress
from worker.connection_supervisor import ConnectionSupervisor
from worker.contact_reconciler import ContactReconciler, ReconciliationAbortedError
from worker.database_access import run_in_database_thread
from worker.message_drainer import MessageDrainer
from worker.node_clock import check_and_correct_node_clock
from worker.node_gateway import NodeGateway, NodeGatewayError, NodeRejectedCommandError
from worker.node_quiet_period import wait_until_no_packet_awaits_acknowledgement
from worker.node_setup_steps import NodeSetupSteps, export_verified_contact_card
from worker.pairing_advertiser import PairingAdvertiser, PairingRefusedError
from worker.relay_modes import find_node_command_refusal, load_active_setup_run_summary
from worker.settings_drift import ExpectedNodeSettings, ReportedNodeSettings, find_settings_drift
from worker.worker_queries import read_node_command_ids_left_running_by
from worker.worker_state import WorkerState
from worker.worker_timing import WorkerTiming

logger = logging.getLogger(__name__)

ERR_CODE_TABLE_FULL = 3
COMMAND_END_UNKNOWN_ERROR_MESSAGE = "The relay worker failed while it ran this command; how it ended is unknown."
CLAIMED_AT_SHUTDOWN_ERROR_MESSAGE = "The relay worker stopped before this command could run."

type NodeCommandHandler = Callable[[NodeCommand, NodeCommandProgress], Awaitable[dict[str, Any] | None]]


class NodeCommandExecutor:
    def __init__(
        self,
        *,
        worker_instance_id: UUID,
        gateway: NodeGateway,
        connection_supervisor: ConnectionSupervisor,
        message_drainer: MessageDrainer,
        contact_reconciler: ContactReconciler,
        pairing_advertiser: PairingAdvertiser,
        node_setup_steps: NodeSetupSteps,
        worker_state: WorkerState,
        clock: Clock,
        timing: WorkerTiming,
    ) -> None:
        self._worker_instance_id = worker_instance_id
        self._gateway = gateway
        self._connection_supervisor = connection_supervisor
        self._message_drainer = message_drainer
        self._contact_reconciler = contact_reconciler
        self._pairing_advertiser = pairing_advertiser
        self._node_setup_steps = node_setup_steps
        self._worker_state = worker_state
        self._signals = worker_state.signals
        self._clock = clock
        self._timing = timing
        self.command_is_running = False
        self._command_finished = asyncio.Event()
        self._handlers_by_kind: dict[NodeCommand.Kind, NodeCommandHandler] = {
            NodeCommand.Kind.READ_NODE_INFORMATION: node_setup_steps.read_node_information,
            NodeCommand.Kind.FACTORY_RESET: node_setup_steps.factory_reset,
            NodeCommand.Kind.CONFIGURE_NODE: node_setup_steps.configure_node,
            NodeCommand.Kind.APPLY_CONFIGURED_SETTINGS: self._apply_configured_settings,
            NodeCommand.Kind.REBOOT_NODE: self._reboot_node,
            NodeCommand.Kind.SEND_ADVERT: self._send_advert,
            NodeCommand.Kind.EXPORT_CONTACT_CARD: self._export_contact_card,
            NodeCommand.Kind.START_PAIRING: self._start_pairing,
            NodeCommand.Kind.STOP_PAIRING: self._stop_pairing,
            NodeCommand.Kind.RECONCILE_CONTACTS: self._reconcile_contacts,
        }

    async def run(self) -> None:
        await self._interrupt_commands_this_worker_left_running()
        while not self._signals.is_shutting_down:
            await wait_for_any_event_or_timeout(
                self._clock,
                [self._signals.commands_available, self._signals.shutdown_requested],
                self._timing.database_sweep_seconds,
            )
            self._signals.commands_available.clear()
            await self.run_pending_commands()

    async def wait_for_running_command(self, timeout_seconds: float) -> bool:
        """At shutdown: True once no command runs, False when it still ran after the timeout."""
        if not self.command_is_running:
            return True
        return await wait_for_any_event_or_timeout(self._clock, [self._command_finished], timeout_seconds)

    async def run_pending_commands(self) -> None:
        while not self._signals.is_shutting_down:
            if not self._worker_state.is_node_connected:
                await self._expire_pending_commands()
                return
            await self._connection_supervisor.recompute_relay_mode()
            node_command = await run_in_database_thread(
                claim_next_node_command, self._worker_instance_id, self._clock.now()
            )
            if node_command is None:
                return
            if self._signals.is_shutting_down:
                await self._finish(
                    node_command, NodeCommand.State.INTERRUPTED, error_message=CLAIMED_AT_SHUTDOWN_ERROR_MESSAGE
                )
                return
            await self.execute(node_command)

    async def _interrupt_commands_this_worker_left_running(self) -> None:
        left_running_command_ids = await run_in_database_thread(
            read_node_command_ids_left_running_by, self._worker_instance_id
        )
        for node_command_id in left_running_command_ids:
            logger.warning(
                "Node command %d was left running after an internal error; it is marked interrupted.",
                node_command_id,
            )
            await run_in_database_thread(
                finish_node_command,
                node_command_id,
                NodeCommand.State.INTERRUPTED,
                self._clock.now(),
                None,
                COMMAND_END_UNKNOWN_ERROR_MESSAGE,
            )

    async def _expire_pending_commands(self) -> None:
        for expired_command in await run_in_database_thread(expire_pending_node_commands, self._clock.now()):
            logger.info(
                "Node command %d (%s) expired while the node was not connected.",
                expired_command.pk,
                expired_command.kind,
            )

    async def execute(self, node_command: NodeCommand) -> None:
        self.command_is_running = True
        self._command_finished.clear()
        try:
            await self._execute_claimed_command(node_command)
        finally:
            self.command_is_running = False
            self._command_finished.set()
            await self._recompute_relay_mode_after_command()

    async def _execute_claimed_command(self, node_command: NodeCommand) -> None:
        active_setup_run = await run_in_database_thread(load_active_setup_run_summary)
        refusal = find_node_command_refusal(node_command, self._worker_state.relay_mode, active_setup_run)
        if refusal is not None:
            logger.info("Node command %d (%s) refused: %s", node_command.pk, node_command.kind, refusal)
            await self._finish(node_command, NodeCommand.State.FAILED, error_message=refusal)
            return

        logger.info("Running node command %d (%s).", node_command.pk, node_command.kind)
        progress = NodeCommandProgress(node_command_id=node_command.pk, clock=self._clock)
        handler = self._handlers_by_kind[NodeCommand.Kind(node_command.kind)]
        try:
            result = await handler(node_command, progress)
        except (NodeCommandFailedError, NodeGatewayError, PairingRefusedError, ReconciliationAbortedError) as failure:
            logger.warning("Node command %d (%s) failed: %s", node_command.pk, node_command.kind, failure)
            await self._finish(node_command, NodeCommand.State.FAILED, error_message=str(failure))
            return
        except Exception as internal_error:
            logger.exception("Node command %d (%s) failed with an internal error.", node_command.pk, node_command.kind)
            self._worker_state.record_error(
                f"Node command {node_command.kind} failed with an internal error: {internal_error}"
            )
            await self._finish(
                node_command,
                NodeCommand.State.FAILED,
                error_message=f"Internal error in the relay worker: {internal_error}",
            )
            return
        await self._finish(node_command, NodeCommand.State.SUCCEEDED, result=result)
        logger.info("Node command %d (%s) succeeded.", node_command.pk, node_command.kind)

    async def _finish(
        self,
        node_command: NodeCommand,
        state: NodeCommand.State,
        *,
        result: dict[str, Any] | None = None,
        error_message: str = "",
    ) -> None:
        await run_in_database_thread(
            finish_node_command, node_command.pk, state, self._clock.now(), result, error_message
        )

    async def _recompute_relay_mode_after_command(self) -> None:
        try:
            await self._connection_supervisor.recompute_relay_mode()
        except Exception:
            logger.exception("The relay mode could not be recomputed after a node command.")

    # ----- the kinds ------------------------------------------------------------------------

    async def _apply_configured_settings(
        self, _node_command: NodeCommand, progress: NodeCommandProgress
    ) -> dict[str, Any] | None:
        node_configuration = await run_in_database_thread(load_node_configuration)
        if node_configuration is None:
            raise NodeCommandFailedError("The node has not been set up, so there is nothing to apply.")

        async with self._worker_state.pause_sending("the configured settings are being applied"):
            await progress.start("send_settings")
            await self._gateway.set_node_name(node_configuration.node_name)
            await self._gateway.set_radio_parameters(
                frequency_kilohertz=node_configuration.radio_frequency_kilohertz,
                bandwidth_hertz=node_configuration.radio_bandwidth_hertz,
                spreading_factor=node_configuration.radio_spreading_factor,
                coding_rate=node_configuration.radio_coding_rate,
                client_repeat=node_configuration.radio_client_repeat,
            )
            await self._gateway.set_transmit_power(node_configuration.radio_transmit_power_dbm)
            await self._gateway.set_path_hash_size(node_configuration.routing_path_hash_size)
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
            await progress.finish("send_settings")

            await progress.start("read_back")
            reported_settings = ReportedNodeSettings(
                self_information=await self._gateway.read_self_information(),
                device_information=await self._gateway.query_device(),
                auto_add_configuration=await self._gateway.read_auto_add_configuration(),
            )
        remaining_drift = find_settings_drift(
            ExpectedNodeSettings.from_node_configuration(node_configuration), reported_settings
        )
        self._worker_state.runtime_status.settings_drift = [drift.to_json() for drift in remaining_drift]
        self._worker_state.report_status_change()
        if remaining_drift:
            drift_description = ", ".join(drift.key.value for drift in remaining_drift)
            raise await progress.fail("read_back", f"The node still differs in: {drift_description}.")
        await progress.finish("read_back", "Every setting matches.")
        return {"remaining_drift": []}

    async def _reboot_node(self, _node_command: NodeCommand, progress: NodeCommandProgress) -> dict[str, Any] | None:
        if self._worker_state.is_running:
            await progress.start("receive_waiting_messages")
            received_count = await self._message_drainer.drain_offline_queue(only_while_running=True)
            await progress.finish("receive_waiting_messages", f"{received_count} waiting messages received.")

        async with self._worker_state.pause_sending("the node is about to reboot"):
            await progress.start("wait_for_acknowledgements")
            is_quiet = await wait_until_no_packet_awaits_acknowledgement(
                self._clock, self._timing, self._timing.node_restart_quiet_wait_seconds, self._worker_state
            )
            await progress.finish(
                "wait_for_acknowledgements", "" if is_quiet else "Some packets still awaited an ACK; rebooting anyway."
            )
            generation_before_reboot = self._connection_supervisor.connection_generation
            await progress.start("reboot")
            await self._gateway.reboot()
            await progress.finish("reboot")

        await progress.start("wait_for_disconnect")
        if not await self._connection_supervisor.wait_for_disconnection(
            generation_before_reboot, self._timing.reboot_disconnect_wait_seconds
        ):
            raise await progress.fail("wait_for_disconnect", "The node did not restart: its link never dropped.")
        await progress.finish("wait_for_disconnect")

        await progress.start("wait_for_reconnect")
        if not await self._connection_supervisor.wait_for_reconnection(
            generation_before_reboot, self._timing.reconnect_after_restart_wait_seconds
        ):
            raise await progress.fail("wait_for_reconnect", "The node did not come back after the reboot.")
        relay_mode = await self._connection_supervisor.recompute_relay_mode()
        await progress.finish("wait_for_reconnect", f"Relay mode {relay_mode.label.lower()}.")
        return {
            "connection_generation": self._connection_supervisor.connection_generation,
            "relay_mode": relay_mode.value,
        }

    async def _send_advert(self, node_command: NodeCommand, progress: NodeCommandProgress) -> dict[str, Any] | None:
        advert_flood = bool(node_command.arguments.get("flood", False))
        await progress.start("check_clock")
        await check_and_correct_node_clock(self._gateway, self._clock, self._timing)
        await progress.finish("check_clock")
        await progress.start("send_advert")
        try:
            await self._gateway.send_advert(flood=advert_flood)
        except NodeRejectedCommandError as rejection:
            if rejection.error_code != ERR_CODE_TABLE_FULL:
                raise
            await self._clock.sleep(self._timing.advert_retry_seconds)
            await self._gateway.send_advert(flood=advert_flood)
        await progress.finish(
            "send_advert", "A flood advert was sent." if advert_flood else "A zero-hop advert was sent."
        )
        return None

    async def _export_contact_card(
        self, _node_command: NodeCommand, progress: NodeCommandProgress
    ) -> dict[str, Any] | None:
        node_configuration = await run_in_database_thread(load_node_configuration)
        if node_configuration is None:
            raise NodeCommandFailedError("The node has not been set up.")
        await progress.start("export_contact_card")
        try:
            contact_card_uri = await export_verified_contact_card(self._gateway, node_configuration.node_public_key)
        except InvalidContactCardError as card_error:
            raise await progress.fail("export_contact_card", str(card_error)) from card_error
        await run_in_database_thread(update_node_setting, NodeSettingKey.NODE_CONTACT_CARD_URI, contact_card_uri)
        await progress.finish("export_contact_card")
        return {"contact_card_uri": contact_card_uri}

    async def _start_pairing(self, node_command: NodeCommand, progress: NodeCommandProgress) -> dict[str, Any] | None:
        await progress.start("start_pairing")
        pairing_session_id = await self._pairing_advertiser.start_pairing(node_command)
        await progress.finish("start_pairing", "The first advert was sent.")
        return {"pairing_session_id": pairing_session_id}

    async def _stop_pairing(self, node_command: NodeCommand, progress: NodeCommandProgress) -> dict[str, Any] | None:
        """The panel usually stopped the session already; stopping it again changes nothing."""
        pairing_session_id = node_command.arguments.get("pairing_session_id")
        if pairing_session_id is not None:
            await run_in_database_thread(stop_pairing_session, int(pairing_session_id), self._clock.now())
        self._signals.pairing_changed.set()
        await progress.finish("stop_pairing")
        return None

    async def _reconcile_contacts(
        self, _node_command: NodeCommand, progress: NodeCommandProgress
    ) -> dict[str, Any] | None:
        await progress.start("reconcile_contacts")
        summary = await self._contact_reconciler.reconcile_contacts()
        await progress.finish(
            "reconcile_contacts",
            f"{summary.added_count} added, {summary.removed_count} removed, {summary.failed_count} failed.",
        )
        return summary.to_json()
