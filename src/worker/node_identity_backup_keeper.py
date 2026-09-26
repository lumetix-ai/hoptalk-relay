"""Keeping the encrypted backup of the configured node's private key (node.node_identity_backups) up to date.

At the end of every handshake in relay mode running, the backup must decrypt and belong to
node.public_key. When there is none, when it no longer decrypts (SECRET_KEY changed) or when it
belongs to another key, the key is exported from the node, the node's public key is read right
after it under the same command lock, and the backup is stored only when the exported key
derives that public key and it is still node.public_key. A firmware built without the export
answers DISABLED: that is recorded in worker_status and warned about once per connection, and
tried again only at the next connection. In the other relay modes the attached node may not be
the configured one, so the backup's state is only reported.

The configure step of a setup run takes the backup of a new identity with
export_private_key_of(), and stores it together with the configuration.
"""

import logging
from dataclasses import dataclass

from django.db import DatabaseError

from node.models import WorkerStatus
from node.node_identity_backups import (
    NodeIdentityBackupStatus,
    NodePrivateKey,
    read_configured_node_identity_backup,
    store_node_identity_backup,
)
from worker.clock import Clock
from worker.database_access import run_in_database_thread
from worker.node_gateway import NodeGateway, NodeGatewayError, NodeNotConnectedError, PrivateKeyExportDisabled
from worker.worker_state import WorkerState

logger = logging.getLogger(__name__)

NodeIdentityBackupState = WorkerStatus.NodeIdentityBackupState
PUBLIC_KEY_PREFIX_LENGTH = 12

EXPORT_DISABLED_DESCRIPTION = (
    "This node's firmware does not allow exporting its private key (it was built without ENABLE_PRIVATE_KEY_EXPORT)."
)
NOT_BACKED_UP_CONSEQUENCE = "The relay's identity is not backed up, so a reconfiguration would give it a new identity."


@dataclass(frozen=True, kw_only=True)
class PrivateKeyExportAttempt:
    # Set only when the node exported the key of the expected identity.
    private_key: NodePrivateKey | None
    # STORED when the key was exported; EXPORT_DISABLED or FAILED otherwise.
    backup_state: WorkerStatus.NodeIdentityBackupState
    failure_description: str = ""


class NodeIdentityBackupKeeper:
    def __init__(self, *, gateway: NodeGateway, worker_state: WorkerState, clock: Clock) -> None:
        self._gateway = gateway
        self._worker_state = worker_state
        self._clock = clock

    async def check_identity_backup(self, relay_mode: WorkerStatus.RelayMode) -> None:
        try:
            configured_backup = await run_in_database_thread(read_configured_node_identity_backup)
        except DatabaseError:
            logger.exception("The identity backup could not be read.")
            return

        configured_public_key = configured_backup.configured_public_key
        if not configured_public_key:
            self._worker_state.record_node_identity_backup_state(NodeIdentityBackupState.NOT_CHECKED)
            return
        if configured_backup.is_restorable:
            self._worker_state.record_node_identity_backup_state(NodeIdentityBackupState.STORED)
            return
        if relay_mode != WorkerStatus.RelayMode.RUNNING:
            backup_is_unreadable = configured_backup.backup_state.status == NodeIdentityBackupStatus.UNREADABLE
            self._worker_state.record_node_identity_backup_state(
                NodeIdentityBackupState.UNREADABLE if backup_is_unreadable else NodeIdentityBackupState.MISSING
            )
            return
        await self._back_up_configured_identity(configured_public_key)

    async def export_private_key_of(self, expected_public_key: str) -> PrivateKeyExportAttempt:
        """The attached node's private key, when the node confirms right after that it is expected_public_key's.

        A lost link is not a failed backup: NodeNotConnectedError ends the handshake or command as it would anywhere.
        """
        try:
            export_outcome = await self._gateway.export_private_key()
        except NodeNotConnectedError:
            raise
        except NodeGatewayError as export_error:
            return PrivateKeyExportAttempt(
                private_key=None,
                backup_state=NodeIdentityBackupState.FAILED,
                failure_description=f"The node's private key could not be exported: {export_error}",
            )
        if isinstance(export_outcome, PrivateKeyExportDisabled):
            return PrivateKeyExportAttempt(
                private_key=None,
                backup_state=NodeIdentityBackupState.EXPORT_DISABLED,
                failure_description=EXPORT_DISABLED_DESCRIPTION,
            )
        if export_outcome.public_key != expected_public_key:
            return PrivateKeyExportAttempt(
                private_key=None,
                backup_state=NodeIdentityBackupState.FAILED,
                failure_description=(
                    f"The node reported key {export_outcome.public_key[:PUBLIC_KEY_PREFIX_LENGTH]} with its private "
                    f"key, not {expected_public_key[:PUBLIC_KEY_PREFIX_LENGTH]}."
                ),
            )
        return PrivateKeyExportAttempt(
            private_key=export_outcome.private_key, backup_state=NodeIdentityBackupState.STORED
        )

    async def _back_up_configured_identity(self, configured_public_key: str) -> None:
        export_attempt = await self.export_private_key_of(configured_public_key)
        if export_attempt.private_key is None:
            logger.warning("%s %s", export_attempt.failure_description, NOT_BACKED_UP_CONSEQUENCE)
            self._worker_state.record_node_identity_backup_state(export_attempt.backup_state)
            return

        try:
            is_stored = await run_in_database_thread(
                store_node_identity_backup, configured_public_key, export_attempt.private_key, self._clock.now()
            )
        except DatabaseError:
            logger.exception("The identity backup could not be stored.")
            self._worker_state.record_node_identity_backup_state(NodeIdentityBackupState.FAILED)
            return
        if not is_stored:
            logger.warning(
                "The identity backup of %s was not stored: node_setting names another node now.",
                configured_public_key[:PUBLIC_KEY_PREFIX_LENGTH],
            )
            self._worker_state.record_node_identity_backup_state(NodeIdentityBackupState.FAILED)
            return
        logger.info(
            "The relay's identity (key %s) was backed up, encrypted with SECRET_KEY.",
            configured_public_key[:PUBLIC_KEY_PREFIX_LENGTH],
        )
        self._worker_state.record_node_identity_backup_state(NodeIdentityBackupState.STORED)
