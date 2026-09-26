"""The setup wizard's node work: reading the node, the factory reset, and the configuration.

The factory reset records its "send the reset frame" step before it writes the frame. If the
command then fails or the worker dies, that entry tells the fallback that the node may already
be reset, and the run reads the node again instead of showing a node that may no longer exist.

The configuration writes every setting as a raw frame of full length, reboots the node so that
nothing lives only in its RAM, reads every value back and compares it with the request, and
replaces node_setting only when all of it matched. From then on the node is the configured one
and relaying resumes.

When the operator keeps the relay's identity, the configuration first imports the backed-up
private key of node.public_key into the reset node, so users keep the relay's contact card. The
run notes the restore before the import frame is sent: from then on the node may hold the
configured key, and the relay mode and a later attempt accept it as the reset node's. A later
attempt that finds the node holding it already goes on with it, whatever the form or the backup
say now, since only another factory reset could take it away. Otherwise the new identity's key is
exported after the read-back and backed up with the configuration.
"""

import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from node.contact_cards import InvalidContactCardError, parse_contact_card_uri
from node.models import NodeCommand, NodeSetupRun, WorkerStatus
from node.node_identity_backups import (
    NodeIdentityBackup,
    NodeIdentityBackupError,
    NodePrivateKey,
    encrypt_node_identity_backup,
    load_private_key_for_restore,
    read_configured_node_identity_backup,
)
from node.node_information import NodeInformation
from node.node_settings import NodeConfiguration
from node.setup_runs import (
    ConfigureNodeStep,
    FactoryResetStep,
    RequestedNodeConfiguration,
    SetupRunTransitionError,
    record_configuration_completed,
    record_factory_reset_succeeded,
    record_identity_restore_started,
    record_node_information_read,
)
from worker.clock import Clock
from worker.command_progress import NodeCommandFailedError, NodeCommandProgress
from worker.connection_supervisor import ConnectionSupervisor
from worker.contact_reconciler import ContactReconciler, ReconciliationAbortedError
from worker.database_access import run_in_database_thread
from worker.message_drainer import MessageDrainer
from worker.node_clock import check_and_correct_node_clock
from worker.node_gateway import (
    DeviceInformation,
    FactoryResetReply,
    NodeGateway,
    NodeGatewayError,
    NodeNotConnectedError,
    PrivateKeyImportDisabled,
    PrivateKeyImportOutcome,
    PrivateKeyImportRefused,
    SelfInformation,
)
from worker.node_identity_backup_keeper import NodeIdentityBackupKeeper
from worker.node_quiet_period import wait_until_no_packet_awaits_acknowledgement
from worker.relay_modes import read_configured_public_key
from worker.settings_drift import ExpectedNodeSettings, ReportedNodeSettings, find_settings_drift
from worker.worker_queries import read_setup_run
from worker.worker_state import WorkerState
from worker.worker_timing import WorkerTiming

logger = logging.getLogger(__name__)

# The well-known key every node pre-configures for the Public channel in slot 0.
PUBLIC_CHANNEL_SECRET = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")
PUBLIC_CHANNEL_INDEX = 0
PRIVATE_CHANNEL_NAME = "Private"
CHANNEL_SECRET_BYTES = 16

# The settings every relay node gets: manual adding on, telemetry denied, location never
# shared, two ACKs for every direct DM, and no automatic adding of any node type.
MANUAL_ADD_CONTACTS = True
TELEMETRY_DENIED = 0
LOCATION_NOT_SHARED = 0
MULTI_ACKS = 2
NO_AUTO_ADD = 0
NO_AUTO_ADD_HOP_LIMIT = 0
CLIENT_REPEAT_OFF = False

NODE_IGNORED_RESET_ERROR = "The node ignored the reset; its firmware may expect a different payload."
NODE_DID_NOT_RETURN_AFTER_RESET_ERROR = (
    "The node did not come back after the reset. Unplug it and plug it in again, then press Retry."
)
IDENTITY_UNCHANGED_ERROR = "The node kept its identity: it was not reset."
NODE_RECONNECTED_BEFORE_RESET_ERROR = (
    "The node was reconnected before the reset was sent, so it was not reset. Confirm the reset again."
)

NodeIdentityBackupState = WorkerStatus.NodeIdentityBackupState
PUBLIC_KEY_PREFIX_LENGTH = 12
ERR_CODE_FILE_INPUT_OUTPUT = 5
ERR_CODE_ILLEGAL_ARGUMENT = 6
CONTINUE_WITH_NEW_IDENTITY_ADVICE = (
    'Untick "Keep the relay\'s identity" to continue with the new identity; every user then adds the new card.'
)
NO_IDENTITY_TO_RESTORE_ERROR = "There is no configured identity to restore: the relay has never been set up."
IMPORT_DISABLED_ERROR = (
    "This node's firmware does not allow importing a private key (it was built without ENABLE_PRIVATE_KEY_IMPORT)."
)
IDENTITY_ALREADY_RESTORED_DETAIL = "The node already holds the relay's identity from an earlier attempt."
IDENTITY_UNKNOWN_AFTER_IMPORT_REASON = "the node's identity is unknown after the private key import"


@dataclass(frozen=True, kw_only=True)
class IdentityBackupOutcome:
    """What the configuration leaves as the relay's identity backup."""

    # A new backup to store with the configuration; None keeps a restored identity's backup.
    new_node_identity_backup: NodeIdentityBackup | None
    backup_state: WorkerStatus.NodeIdentityBackupState


@dataclass(frozen=True, kw_only=True)
class NodeReadBack:
    self_information: SelfInformation
    device_information: DeviceInformation
    reported_settings: ReportedNodeSettings
    channel_zero_name: str
    channel_zero_is_public: bool


class NodeSetupSteps:
    def __init__(
        self,
        *,
        gateway: NodeGateway,
        connection_supervisor: ConnectionSupervisor,
        identity_backup_keeper: NodeIdentityBackupKeeper,
        message_drainer: MessageDrainer,
        contact_reconciler: ContactReconciler,
        worker_state: WorkerState,
        clock: Clock,
        timing: WorkerTiming,
    ) -> None:
        self._gateway = gateway
        self._connection_supervisor = connection_supervisor
        self._identity_backup_keeper = identity_backup_keeper
        self._message_drainer = message_drainer
        self._contact_reconciler = contact_reconciler
        self._worker_state = worker_state
        self._clock = clock
        self._timing = timing

    # ----- read_node_information -----------------------------------------------------------

    async def read_node_information(self, node_command: NodeCommand, progress: NodeCommandProgress) -> dict[str, Any]:
        await progress.start("read_node")
        node_read_back = await self._read_node_back()
        contacts_on_node = await self._gateway.list_contacts()
        node_clock_timestamp = await self._gateway.read_node_clock()
        node_information = build_node_information(
            node_read_back,
            contact_count=len(contacts_on_node),
            node_clock_timestamp=node_clock_timestamp,
            server_clock_timestamp=int(self._clock.now().timestamp()),
        )
        await progress.finish("read_node", f"{node_information.name or 'unnamed'} ({node_information.public_key[:12]})")

        node_information_json = node_information.to_json()
        if node_command.setup_run_id is not None:
            await run_in_database_thread(
                record_node_information_read, node_command.setup_run_id, node_information_json, self._clock.now()
            )
        return node_information_json

    async def _read_node_back(self) -> NodeReadBack:
        self_information = await self._gateway.read_self_information()
        device_information = await self._gateway.query_device()
        auto_add_configuration = await self._gateway.read_auto_add_configuration()
        channel_zero = await self._gateway.read_channel(PUBLIC_CHANNEL_INDEX)
        return NodeReadBack(
            self_information=self_information,
            device_information=device_information,
            reported_settings=ReportedNodeSettings(
                self_information=self_information,
                device_information=device_information,
                auto_add_configuration=auto_add_configuration,
            ),
            channel_zero_name=channel_zero.name,
            channel_zero_is_public=channel_zero.secret == PUBLIC_CHANNEL_SECRET,
        )

    # ----- factory_reset -------------------------------------------------------------------

    async def factory_reset(self, node_command: NodeCommand, progress: NodeCommandProgress) -> dict[str, Any]:
        setup_run = await self._load_setup_run(node_command)
        original_public_key = setup_run.original_public_key

        checked_connection_generation = await self._require_confirmed_node_attached(original_public_key)
        await self._receive_messages_before_reset(original_public_key, progress)
        generation_before_reset = self._connection_supervisor.connection_generation
        if generation_before_reset != checked_connection_generation:
            raise NodeCommandFailedError(NODE_RECONNECTED_BEFORE_RESET_ERROR)
        await self._send_reset_frame(progress)
        await self._wait_for_node_to_disappear(generation_before_reset, original_public_key, progress)

        await progress.start(FactoryResetStep.WAIT_FOR_RECONNECT)
        node_came_back = await self._connection_supervisor.wait_for_reconnection(
            generation_before_reset, self._timing.reconnect_after_restart_wait_seconds
        )
        if not node_came_back:
            raise await progress.fail(FactoryResetStep.WAIT_FOR_RECONNECT, NODE_DID_NOT_RETURN_AFTER_RESET_ERROR)
        await progress.finish(FactoryResetStep.WAIT_FOR_RECONNECT)

        await progress.start(FactoryResetStep.READ_NEW_IDENTITY)
        self_information = await self._gateway.read_self_information()
        if self_information.public_key == original_public_key:
            raise await progress.fail(FactoryResetStep.READ_NEW_IDENTITY, IDENTITY_UNCHANGED_ERROR)
        try:
            await run_in_database_thread(
                record_factory_reset_succeeded, setup_run.pk, self_information.public_key, self._clock.now()
            )
        except SetupRunTransitionError as transition_error:
            raise await progress.fail(FactoryResetStep.READ_NEW_IDENTITY, str(transition_error)) from transition_error
        await progress.finish(
            FactoryResetStep.READ_NEW_IDENTITY, f"The new key starts {self_information.public_key[:12]}."
        )
        await self._connection_supervisor.recompute_relay_mode()
        logger.info("The node was reset; its new key is %s.", self_information.public_key)
        return {"new_public_key": self_information.public_key}

    async def _require_confirmed_node_attached(self, original_public_key: str) -> int:
        """The operator confirmed the reset of the node that was read; another board may have been plugged in since.

        Returns the connection generation the identity was checked on.
        """
        checked_connection_generation = self._connection_supervisor.connection_generation
        attached_public_key = (await self._gateway.read_self_information()).public_key
        if attached_public_key != original_public_key:
            raise NodeCommandFailedError(
                f"A different node is attached (key {attached_public_key[:12]}, expected {original_public_key[:12]}); "
                "it was not reset. Attach the node that was read, or cancel the setup and start again."
            )
        return checked_connection_generation

    async def _receive_messages_before_reset(self, original_public_key: str, progress: NodeCommandProgress) -> None:
        """On a reconfiguration the node holds this relay's messages, which live only in its RAM."""
        step = FactoryResetStep.RECEIVE_WAITING_MESSAGES
        configured_public_key = await run_in_database_thread(read_configured_public_key)
        if not configured_public_key or configured_public_key != original_public_key:
            await progress.skip(step, "The node holds no messages for this relay.")
            return
        await progress.start(step)
        received_count = await self._message_drainer.drain_offline_queue(only_while_running=False)
        await wait_until_no_packet_awaits_acknowledgement(
            self._clock, self._timing, self._timing.node_restart_quiet_wait_seconds, self._worker_state
        )
        await progress.finish(step, f"{received_count} waiting messages received.")

    async def _send_reset_frame(self, progress: NodeCommandProgress) -> None:
        step = FactoryResetStep.SEND_RESET_FRAME
        await progress.start(step)
        try:
            reset_reply, error_code = await self._gateway.factory_reset()
        except NodeNotConnectedError:
            reset_reply, error_code = FactoryResetReply.NO_REPLY, None
        if reset_reply == FactoryResetReply.REFUSED:
            raise await progress.fail(step, f"The node refused the factory reset with error {error_code}.")
        await progress.finish(step)

    async def _wait_for_node_to_disappear(
        self, generation_before_reset: int, original_public_key: str, progress: NodeCommandProgress
    ) -> None:
        step = FactoryResetStep.WAIT_FOR_DISCONNECT
        await progress.start(step)
        node_disappeared = await self._connection_supervisor.wait_for_disconnection(
            generation_before_reset, self._timing.reboot_disconnect_wait_seconds
        )
        if not node_disappeared:
            self_information = await self._gateway.read_self_information()
            if self_information.public_key == original_public_key:
                raise await progress.fail(step, NODE_IGNORED_RESET_ERROR)
        await progress.finish(step)

    # ----- configure_node ------------------------------------------------------------------

    async def configure_node(self, node_command: NodeCommand, progress: NodeCommandProgress) -> dict[str, Any]:
        setup_run = await self._load_setup_run(node_command)
        if setup_run.requested_configuration is None:
            raise NodeCommandFailedError("The setup run holds no requested configuration.")
        requested_configuration = RequestedNodeConfiguration.from_json(setup_run.requested_configuration)
        configured_public_key = await run_in_database_thread(read_configured_public_key)

        attached_public_key = await self._check_identity(setup_run, progress)
        expected_public_key = await self._restore_identity_if_requested(
            setup_run, requested_configuration, configured_public_key, attached_public_key, progress
        )
        identity_is_restored = expected_public_key != setup_run.new_public_key
        await self._run_setting_steps(requested_configuration, progress)
        await self._reboot_and_wait(progress)
        node_read_back = await self._read_back_and_compare(requested_configuration, expected_public_key, progress)
        identity_backup_outcome = await self._back_up_identity(identity_is_restored, expected_public_key, progress)
        contact_card_uri = await self._export_contact_card(expected_public_key, progress)

        node_configuration = build_node_configuration(
            requested_configuration,
            node_read_back,
            contact_card_uri=contact_card_uri,
            setup_run_id=setup_run.pk,
            completed_at=self._clock.now(),
        )
        await self._persist_configuration(setup_run, node_configuration, identity_backup_outcome, progress)
        logger.info("Setup completed: the node %s is configured.", expected_public_key)

        await self._resume_relaying(progress)
        return {
            "node_public_key": expected_public_key,
            "contact_card_uri": contact_card_uri,
            "identity_restored": identity_is_restored,
        }

    async def _persist_configuration(
        self,
        setup_run: NodeSetupRun,
        node_configuration: NodeConfiguration,
        identity_backup_outcome: IdentityBackupOutcome,
        progress: NodeCommandProgress,
    ) -> None:
        """The node was read back on this connection, so it may relay as soon as the configuration is committed."""
        step = ConfigureNodeStep.PERSIST
        await progress.start(step)
        self._connection_supervisor.confirm_node_settings_verified()
        try:
            await run_in_database_thread(
                record_configuration_completed,
                setup_run.pk,
                node_configuration,
                self._clock.now(),
                identity_backup_outcome.new_node_identity_backup,
            )
        except SetupRunTransitionError as transition_error:
            self._connection_supervisor.withdraw_node_settings_verification()
            raise await progress.fail(step, str(transition_error)) from transition_error
        except BaseException:
            self._connection_supervisor.withdraw_node_settings_verification()
            raise
        await progress.finish(step)
        self._worker_state.record_node_identity_backup_state(identity_backup_outcome.backup_state)

    async def _check_identity(self, setup_run: NodeSetupRun, progress: NodeCommandProgress) -> str:
        """The reset node must be attached: with the reset's key, or with the key an earlier attempt restored."""
        step = ConfigureNodeStep.CHECK_IDENTITY
        await progress.start(step)
        attached_public_key = (await self._gateway.read_self_information()).public_key
        if attached_public_key == setup_run.new_public_key:
            await progress.finish(step)
            return attached_public_key
        if holds_restored_identity(setup_run, attached_public_key):
            await progress.finish(step, IDENTITY_ALREADY_RESTORED_DETAIL)
            return attached_public_key
        raise await progress.fail(
            step,
            f"A different node is attached (key {attached_public_key[:PUBLIC_KEY_PREFIX_LENGTH]}, "
            f"expected {setup_run.new_public_key[:PUBLIC_KEY_PREFIX_LENGTH]}).",
        )

    async def _restore_identity_if_requested(
        self,
        setup_run: NodeSetupRun,
        requested_configuration: RequestedNodeConfiguration,
        configured_public_key: str,
        attached_public_key: str,
        progress: NodeCommandProgress,
    ) -> str:
        """The key every later step expects: the configured one once restored, else the reset's new key."""
        step = ConfigureNodeStep.RESTORE_IDENTITY
        if holds_restored_identity(setup_run, attached_public_key):
            await progress.start(step)
            await progress.finish(step, IDENTITY_ALREADY_RESTORED_DETAIL)
            return attached_public_key
        if not requested_configuration.restore_identity:
            await progress.skip(step, "The node keeps the new identity from the reset.")
            return setup_run.new_public_key

        await progress.start(step)
        private_key = await self._load_backed_up_private_key(configured_public_key, progress)
        await self._import_backed_up_private_key(setup_run, configured_public_key, private_key, progress)
        await self._require_restored_identity(setup_run, configured_public_key, progress)
        await progress.finish(
            step, f"The node holds the relay's identity again (key {configured_public_key[:PUBLIC_KEY_PREFIX_LENGTH]})."
        )
        logger.info("The relay's identity %s was restored onto the reset node.", configured_public_key)
        return configured_public_key

    async def _load_backed_up_private_key(
        self, configured_public_key: str, progress: NodeCommandProgress
    ) -> NodePrivateKey:
        step = ConfigureNodeStep.RESTORE_IDENTITY
        if not configured_public_key:
            raise await progress.fail(step, NO_IDENTITY_TO_RESTORE_ERROR)
        try:
            return await run_in_database_thread(load_private_key_for_restore, configured_public_key)
        except NodeIdentityBackupError as backup_error:
            raise await progress.fail(step, f"{backup_error} {CONTINUE_WITH_NEW_IDENTITY_ADVICE}") from backup_error

    async def _import_backed_up_private_key(
        self,
        setup_run: NodeSetupRun,
        configured_public_key: str,
        private_key: NodePrivateKey,
        progress: NodeCommandProgress,
    ) -> None:
        """The run and the progress name the restore before the frame goes out, whatever happens after it."""
        step = ConfigureNodeStep.RESTORE_IDENTITY
        try:
            await run_in_database_thread(record_identity_restore_started, setup_run.pk, configured_public_key)
        except SetupRunTransitionError as transition_error:
            raise await progress.fail(step, str(transition_error)) from transition_error
        await progress.start(step, "Importing the relay's private key into the node.")

        try:
            import_outcome = await self._gateway.import_private_key(private_key)
        except NodeGatewayError as import_error:
            self._reconnect_to_read_the_identity_again(import_error)
            raise await progress.fail(
                step, f"The relay's identity could not be restored: {import_error}"
            ) from import_error
        import_refusal = describe_private_key_import_refusal(import_outcome)
        if import_refusal:
            raise await progress.fail(step, import_refusal)

    async def _require_restored_identity(
        self, setup_run: NodeSetupRun, configured_public_key: str, progress: NodeCommandProgress
    ) -> None:
        step = ConfigureNodeStep.RESTORE_IDENTITY
        try:
            reported_public_key = (await self._gateway.read_self_information()).public_key
        except NodeGatewayError as read_error:
            self._reconnect_to_read_the_identity_again(read_error)
            raise await progress.fail(
                step, f"The node's identity could not be read after the import: {read_error}"
            ) from read_error
        self._connection_supervisor.record_node_identity(reported_public_key)
        if reported_public_key == configured_public_key:
            return
        mismatch_description = (
            f"The node reports key {reported_public_key[:PUBLIC_KEY_PREFIX_LENGTH]} after the import, not the "
            f"relay's key {configured_public_key[:PUBLIC_KEY_PREFIX_LENGTH]}"
        )
        if reported_public_key == setup_run.new_public_key:
            raise await progress.fail(step, f"{mismatch_description}: its identity did not change.")
        raise await progress.fail(step, f"{mismatch_description}.")

    def _reconnect_to_read_the_identity_again(self, gateway_error: NodeGatewayError) -> None:
        """The node may or may not have taken the key, so the key the worker last read may be wrong.

        A new connection reads it again before the relay mode is decided. A lost link does that anyway.
        """
        if isinstance(gateway_error, NodeNotConnectedError):
            return
        self._connection_supervisor.request_reconnect(IDENTITY_UNKNOWN_AFTER_IMPORT_REASON)

    async def _run_setting_steps(
        self, requested_configuration: RequestedNodeConfiguration, progress: NodeCommandProgress
    ) -> None:
        await self._run_step(ConfigureNodeStep.SET_CLOCK, progress, self._set_clock)
        await self._run_step(
            ConfigureNodeStep.SET_NAME, progress, lambda: self._gateway.set_node_name(requested_configuration.node_name)
        )
        await self._run_step(
            ConfigureNodeStep.SET_RADIO,
            progress,
            lambda: self._gateway.set_radio_parameters(
                frequency_kilohertz=requested_configuration.radio_frequency_kilohertz,
                bandwidth_hertz=requested_configuration.radio_bandwidth_hertz,
                spreading_factor=requested_configuration.radio_spreading_factor,
                coding_rate=requested_configuration.radio_coding_rate,
                client_repeat=CLIENT_REPEAT_OFF,
            ),
        )
        await self._run_step(
            ConfigureNodeStep.SET_TRANSMIT_POWER,
            progress,
            lambda: self._gateway.set_transmit_power(requested_configuration.transmit_power_dbm),
        )
        await self._run_step(
            ConfigureNodeStep.SET_PATH_HASH_SIZE,
            progress,
            lambda: self._gateway.set_path_hash_size(requested_configuration.path_hash_size),
        )
        await self._run_step(
            ConfigureNodeStep.SET_OTHER_PARAMETERS,
            progress,
            lambda: self._gateway.set_other_parameters(
                manual_add_contacts=MANUAL_ADD_CONTACTS,
                telemetry_modes=TELEMETRY_DENIED,
                advert_location_policy=LOCATION_NOT_SHARED,
                multi_acks=MULTI_ACKS,
            ),
        )
        await self._run_step(
            ConfigureNodeStep.SET_AUTO_ADD_CONFIGURATION,
            progress,
            lambda: self._gateway.set_auto_add_configuration(
                configuration=NO_AUTO_ADD, maximum_hops=NO_AUTO_ADD_HOP_LIMIT
            ),
        )
        if requested_configuration.replace_public_channel:
            await self._run_step(
                ConfigureNodeStep.REPLACE_PUBLIC_CHANNEL,
                progress,
                lambda: self._gateway.set_channel(
                    PUBLIC_CHANNEL_INDEX, PRIVATE_CHANNEL_NAME, secrets.token_bytes(CHANNEL_SECRET_BYTES)
                ),
            )
        else:
            await progress.skip(ConfigureNodeStep.REPLACE_PUBLIC_CHANNEL, "The Public channel is kept.")

    async def _set_clock(self) -> None:
        await check_and_correct_node_clock(self._gateway, self._clock, self._timing)

    async def _run_step(
        self, step: ConfigureNodeStep, progress: NodeCommandProgress, run_node_calls: Callable[[], Awaitable[None]]
    ) -> None:
        await progress.start(step)
        try:
            await run_node_calls()
        except (NodeGatewayError, ValueError) as step_error:
            raise await progress.fail(step, str(step_error)) from step_error
        await progress.finish(step)

    async def _reboot_and_wait(self, progress: NodeCommandProgress) -> None:
        """A freshly reset node holds no contacts, so whatever the drain finds is dropped with it."""
        step = ConfigureNodeStep.REBOOT
        await progress.start(step)
        await self._message_drainer.drain_offline_queue(only_while_running=False)
        generation_before_reboot = self._connection_supervisor.connection_generation
        try:
            await self._gateway.reboot()
        except NodeGatewayError as reboot_error:
            raise await progress.fail(step, str(reboot_error)) from reboot_error
        node_came_back = await self._connection_supervisor.wait_for_reconnection(
            generation_before_reboot, self._timing.reconnect_after_restart_wait_seconds
        )
        if not node_came_back:
            raise await progress.fail(step, "The node did not come back after the reboot.")
        await progress.finish(step)

    async def _read_back_and_compare(
        self,
        requested_configuration: RequestedNodeConfiguration,
        expected_public_key: str,
        progress: NodeCommandProgress,
    ) -> NodeReadBack:
        step = ConfigureNodeStep.READ_BACK
        await progress.start(step)
        try:
            node_read_back = await self._read_node_back()
        except NodeGatewayError as read_error:
            raise await progress.fail(step, str(read_error)) from read_error

        mismatches = find_read_back_mismatches(requested_configuration, node_read_back, expected_public_key)
        if mismatches:
            raise await progress.fail(step, "The node does not hold the requested settings: " + "; ".join(mismatches))
        await progress.finish(step)
        return node_read_back

    async def _back_up_identity(
        self, identity_is_restored: bool, expected_public_key: str, progress: NodeCommandProgress
    ) -> IdentityBackupOutcome:
        """A firmware that refuses the export costs the backup only, never the configuration.

        A restored identity is backed up again only when its backup no longer opens.
        """
        step = ConfigureNodeStep.BACK_UP_IDENTITY
        if identity_is_restored:
            configured_backup = await run_in_database_thread(read_configured_node_identity_backup)
            if configured_backup.backup_state.is_stored_for(expected_public_key):
                await progress.skip(step, "The stored backup already holds the relay's identity.")
                return IdentityBackupOutcome(new_node_identity_backup=None, backup_state=NodeIdentityBackupState.STORED)

        await progress.start(step)
        try:
            export_attempt = await self._identity_backup_keeper.export_private_key_of(expected_public_key)
        except NodeGatewayError as link_error:
            raise await progress.fail(step, str(link_error)) from link_error
        if export_attempt.private_key is None:
            logger.warning("%s The node's identity is not backed up.", export_attempt.failure_description)
            await progress.skip(step, f"{export_attempt.failure_description} The node's identity is not backed up.")
            return IdentityBackupOutcome(new_node_identity_backup=None, backup_state=export_attempt.backup_state)

        new_node_identity_backup = encrypt_node_identity_backup(
            expected_public_key, export_attempt.private_key, self._clock.now()
        )
        await progress.finish(step, "Encrypted with SECRET_KEY; it is saved with the configuration.")
        return IdentityBackupOutcome(
            new_node_identity_backup=new_node_identity_backup, backup_state=NodeIdentityBackupState.STORED
        )

    async def _export_contact_card(self, expected_public_key: str, progress: NodeCommandProgress) -> str:
        step = ConfigureNodeStep.EXPORT_CONTACT_CARD
        await progress.start(step)
        try:
            contact_card_uri = await export_verified_contact_card(self._gateway, expected_public_key)
        except (NodeGatewayError, InvalidContactCardError, NodeCommandFailedError) as export_error:
            raise await progress.fail(step, str(export_error)) from export_error
        await progress.finish(step)
        return contact_card_uri

    async def _resume_relaying(self, progress: NodeCommandProgress) -> None:
        """Errors here are reported, but setup stays completed: the node is configured."""
        step = ConfigureNodeStep.RESUME
        await progress.start(step)
        try:
            await self._connection_supervisor.recompute_relay_mode()
            summary = await self._contact_reconciler.reconcile_contacts()
            await self._message_drainer.drain_offline_queue(only_while_running=True)
        except (ReconciliationAbortedError, NodeGatewayError) as resume_error:
            error_message = f"Relaying resumed with a problem: {resume_error}"
            logger.error(error_message)
            self._worker_state.record_error(error_message)
            await progress.fail(step, error_message)
            return
        finally:
            self._worker_state.signals.sender_wakeup.set()
        await progress.finish(step, f"{summary.added_count} contacts added to the node.")

    async def _load_setup_run(self, node_command: NodeCommand) -> NodeSetupRun:
        setup_run = None
        if node_command.setup_run_id is not None:
            setup_run = await run_in_database_thread(read_setup_run, node_command.setup_run_id)
        if setup_run is None:
            raise NodeCommandFailedError("The command's setup run no longer exists.")
        return setup_run


def holds_restored_identity(setup_run: NodeSetupRun, attached_public_key: str) -> bool:
    """The node took the configured key in an earlier attempt of this run."""
    return bool(setup_run.restored_public_key) and attached_public_key == setup_run.restored_public_key


def describe_private_key_import_refusal(import_outcome: PrivateKeyImportOutcome) -> str:
    """Why the node did not take the key, for the operator; "" when it did."""
    if isinstance(import_outcome, PrivateKeyImportDisabled):
        return f"{IMPORT_DISABLED_ERROR} {CONTINUE_WITH_NEW_IDENTITY_ADVICE}"
    if not isinstance(import_outcome, PrivateKeyImportRefused):
        return ""
    if import_outcome.error_code == ERR_CODE_ILLEGAL_ARGUMENT:
        return f"The node refused the relay's private key as invalid (error 6). {CONTINUE_WITH_NEW_IDENTITY_ADVICE}"
    if import_outcome.error_code == ERR_CODE_FILE_INPUT_OUTPUT:
        return "The node could not save the relay's private key to its flash (error 5). Try again."
    return f"The node refused the relay's private key with error {import_outcome.error_code}."


async def export_verified_contact_card(gateway: NodeGateway, expected_public_key: str) -> str:
    """The node's own card, its signature verified and its key the expected one."""
    contact_card_uri = await gateway.export_own_contact_card()
    contact_card = parse_contact_card_uri(contact_card_uri)
    if contact_card.public_key != expected_public_key:
        raise NodeCommandFailedError(
            f"The exported card carries key {contact_card.public_key[:12]}, not {expected_public_key[:12]}."
        )
    return contact_card.card_uri


def build_expected_node_settings(requested_configuration: RequestedNodeConfiguration) -> ExpectedNodeSettings:
    return ExpectedNodeSettings(
        node_name=requested_configuration.node_name,
        radio_frequency_kilohertz=requested_configuration.radio_frequency_kilohertz,
        radio_bandwidth_hertz=requested_configuration.radio_bandwidth_hertz,
        radio_spreading_factor=requested_configuration.radio_spreading_factor,
        radio_coding_rate=requested_configuration.radio_coding_rate,
        radio_transmit_power_dbm=requested_configuration.transmit_power_dbm,
        radio_client_repeat=CLIENT_REPEAT_OFF,
        routing_path_hash_size=requested_configuration.path_hash_size,
        messaging_multi_acks=MULTI_ACKS,
        contacts_manual_add=MANUAL_ADD_CONTACTS,
        contacts_auto_add_configuration=NO_AUTO_ADD,
        contacts_auto_add_maximum_hops=NO_AUTO_ADD_HOP_LIMIT,
        privacy_advert_location_policy=LOCATION_NOT_SHARED,
        privacy_telemetry_modes=TELEMETRY_DENIED,
    )


def build_node_configuration(
    requested_configuration: RequestedNodeConfiguration,
    node_read_back: NodeReadBack,
    *,
    contact_card_uri: str,
    setup_run_id: int,
    completed_at: datetime,
) -> NodeConfiguration:
    device_information = node_read_back.device_information
    return NodeConfiguration(
        node_public_key=node_read_back.self_information.public_key,
        node_name=requested_configuration.node_name,
        node_firmware_version=device_information.firmware_version,
        node_firmware_build=device_information.firmware_build,
        node_model=device_information.model,
        node_protocol_version=device_information.protocol_version,
        node_maximum_contacts=device_information.maximum_contacts,
        node_contact_card_uri=contact_card_uri,
        radio_preset_title=requested_configuration.radio_preset_title,
        radio_frequency_kilohertz=requested_configuration.radio_frequency_kilohertz,
        radio_bandwidth_hertz=requested_configuration.radio_bandwidth_hertz,
        radio_spreading_factor=requested_configuration.radio_spreading_factor,
        radio_coding_rate=requested_configuration.radio_coding_rate,
        radio_transmit_power_dbm=requested_configuration.transmit_power_dbm,
        radio_client_repeat=CLIENT_REPEAT_OFF,
        routing_path_hash_size=requested_configuration.path_hash_size,
        messaging_multi_acks=MULTI_ACKS,
        contacts_manual_add=MANUAL_ADD_CONTACTS,
        contacts_auto_add_configuration=NO_AUTO_ADD,
        contacts_auto_add_maximum_hops=NO_AUTO_ADD_HOP_LIMIT,
        privacy_advert_location_policy=LOCATION_NOT_SHARED,
        privacy_telemetry_modes=TELEMETRY_DENIED,
        channels_public_channel_replaced=requested_configuration.replace_public_channel,
        setup_completed_at=completed_at,
        setup_run_id=setup_run_id,
    )


def find_read_back_mismatches(
    requested_configuration: RequestedNodeConfiguration, node_read_back: NodeReadBack, expected_public_key: str
) -> list[str]:
    """Every value the configuration wrote, compared with what the node reports after its reboot."""
    mismatches: list[str] = []
    if node_read_back.self_information.public_key != expected_public_key:
        mismatches.append(f"the node's key is {node_read_back.self_information.public_key[:12]}")

    expected_settings = build_expected_node_settings(requested_configuration)
    for drift in find_settings_drift(expected_settings, node_read_back.reported_settings):
        mismatches.append(f"{drift.key.value} is {drift.actual!r}, requested {drift.expected!r}")

    if requested_configuration.replace_public_channel and (
        node_read_back.channel_zero_is_public or node_read_back.channel_zero_name != PRIVATE_CHANNEL_NAME
    ):
        mismatches.append(f"channel 0 is still {node_read_back.channel_zero_name!r}")
    return mismatches


def build_node_information(
    node_read_back: NodeReadBack, *, contact_count: int, node_clock_timestamp: int, server_clock_timestamp: int
) -> NodeInformation:
    self_information = node_read_back.self_information
    device_information = node_read_back.device_information
    auto_add_configuration = node_read_back.reported_settings.auto_add_configuration
    return NodeInformation(
        public_key=self_information.public_key,
        name=self_information.name,
        firmware_version=device_information.firmware_version,
        firmware_build=device_information.firmware_build,
        model=device_information.model,
        protocol_version=device_information.protocol_version,
        maximum_contacts=device_information.maximum_contacts,
        radio_frequency_kilohertz=self_information.radio_frequency_kilohertz,
        radio_bandwidth_hertz=self_information.radio_bandwidth_hertz,
        radio_spreading_factor=self_information.radio_spreading_factor,
        radio_coding_rate=self_information.radio_coding_rate,
        client_repeat=device_information.client_repeat,
        transmit_power_dbm=self_information.transmit_power_dbm,
        maximum_transmit_power_dbm=self_information.maximum_transmit_power_dbm,
        path_hash_size=device_information.path_hash_size,
        multi_acks=self_information.multi_acks,
        manual_add_contacts=self_information.manual_add_contacts,
        auto_add_configuration=auto_add_configuration.configuration,
        auto_add_maximum_hops=auto_add_configuration.maximum_hops,
        advert_location_policy=self_information.advert_location_policy,
        telemetry_modes=self_information.telemetry_modes,
        contact_count=contact_count,
        node_clock_timestamp=node_clock_timestamp,
        server_clock_timestamp=server_clock_timestamp,
        channel_zero_name=node_read_back.channel_zero_name,
        channel_zero_is_public=node_read_back.channel_zero_is_public,
    )
