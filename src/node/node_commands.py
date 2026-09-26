"""The node command queue between the panel and the worker.

The panel creates a command and notifies the worker in one transaction; the worker expires
stale commands, claims the oldest pending one with FOR UPDATE SKIP LOCKED, reports progress
after every step and finishes it. A command runs at most once and never after its expiry,
and at most one runs at a time.

A setup run's command that ends failed, interrupted or expired moves its run back in the same
transaction (node.setup_runs), so run and command never disagree.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction

from node import setup_runs
from node.models import NodeCommand, NodeSetupRun
from node.notification_channels import NotificationChannel, notify_relay_worker

# Commands that must not run long after the operator asked for them expire after a minute;
# the others after five minutes.
SHORT_COMMAND_LIFETIME_SECONDS = 60
DEFAULT_COMMAND_LIFETIME_SECONDS = 300
COMMAND_LIFETIME_SECONDS_BY_KIND: Mapping[NodeCommand.Kind, int] = {
    NodeCommand.Kind.READ_NODE_INFORMATION: DEFAULT_COMMAND_LIFETIME_SECONDS,
    NodeCommand.Kind.FACTORY_RESET: SHORT_COMMAND_LIFETIME_SECONDS,
    NodeCommand.Kind.CONFIGURE_NODE: SHORT_COMMAND_LIFETIME_SECONDS,
    NodeCommand.Kind.APPLY_CONFIGURED_SETTINGS: DEFAULT_COMMAND_LIFETIME_SECONDS,
    NodeCommand.Kind.REBOOT_NODE: SHORT_COMMAND_LIFETIME_SECONDS,
    NodeCommand.Kind.SEND_ADVERT: DEFAULT_COMMAND_LIFETIME_SECONDS,
    NodeCommand.Kind.EXPORT_CONTACT_CARD: DEFAULT_COMMAND_LIFETIME_SECONDS,
    NodeCommand.Kind.START_PAIRING: SHORT_COMMAND_LIFETIME_SECONDS,
    NodeCommand.Kind.STOP_PAIRING: DEFAULT_COMMAND_LIFETIME_SECONDS,
    NodeCommand.Kind.RECONCILE_CONTACTS: DEFAULT_COMMAND_LIFETIME_SECONDS,
}

NODE_COMMAND_RETENTION_DAYS = 90

EXPIRED_COMMAND_ERROR_MESSAGE = (
    "The relay worker did not start this command in time: it was offline, or the node was not connected."
)
INTERRUPTED_COMMAND_ERROR_MESSAGE = "The relay worker restarted while this command was running."

UNSUCCESSFUL_STATES = frozenset({NodeCommand.State.FAILED, NodeCommand.State.INTERRUPTED, NodeCommand.State.EXPIRED})


class ProgressStepState(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, kw_only=True)
class ProgressStep:
    """One entry of node_commands.progress: {"step", "state", "detail", "at"}."""

    step: str
    state: ProgressStepState
    detail: str
    at: datetime

    def to_json(self) -> dict[str, str]:
        return {"step": self.step, "state": self.state.value, "detail": self.detail, "at": self.at.isoformat()}

    @classmethod
    def from_json(cls, stored_json: Mapping[str, Any]) -> ProgressStep:
        return cls(
            step=str(stored_json["step"]),
            state=ProgressStepState(stored_json["state"]),
            detail=str(stored_json.get("detail", "")),
            at=datetime.fromisoformat(str(stored_json["at"])),
        )


def read_progress_steps(node_command: NodeCommand) -> list[ProgressStep]:
    return [ProgressStep.from_json(stored_step) for stored_step in node_command.progress]


def create_node_command(
    kind: NodeCommand.Kind,
    arguments: Mapping[str, Any],
    now: datetime,
    setup_run: NodeSetupRun | None = None,
) -> NodeCommand:
    """Insert a pending command with the lifetime of its kind and notify relay_node_commands with its id.

    Runs in the caller's transaction when there is one; the notification is delivered at commit.
    """
    with transaction.atomic():
        node_command = NodeCommand.objects.create(
            kind=kind,
            arguments=dict(arguments),
            state=NodeCommand.State.PENDING,
            setup_run=setup_run,
            created_at=now,
            expires_at=now + timedelta(seconds=COMMAND_LIFETIME_SECONDS_BY_KIND[kind]),
        )
        notify_relay_worker(NotificationChannel.NODE_COMMANDS, str(node_command.pk))
    return node_command


def expire_pending_node_commands(now: datetime) -> list[NodeCommand]:
    """Move pending commands past expires_at to expired and move their setup runs back.

    The worker calls it before every claim; the panel calls it when it shows a pending command
    past its expiry, so that one does not stay pending while the worker is away. Returns the
    commands it expired.
    """
    with transaction.atomic():
        expired_commands = list(
            NodeCommand.objects.select_for_update(skip_locked=True)
            .filter(state=NodeCommand.State.PENDING, expires_at__lte=now)
            .order_by("id")
        )
        for expired_command in expired_commands:
            expired_command.state = NodeCommand.State.EXPIRED
            expired_command.finished_at = now
            expired_command.error_message = EXPIRED_COMMAND_ERROR_MESSAGE
            expired_command.save(update_fields=["state", "finished_at", "error_message"])
            if expired_command.setup_run_id is not None:
                setup_runs.fall_back_after_unsuccessful_command(expired_command, now)
    return expired_commands


def claim_next_node_command(worker_instance_id: UUID, now: datetime) -> NodeCommand | None:
    """Expire, then claim the oldest pending command (FOR UPDATE SKIP LOCKED) and mark it running."""
    with transaction.atomic():
        expire_pending_node_commands(now)
        node_command = (
            NodeCommand.objects.select_for_update(skip_locked=True)
            .filter(state=NodeCommand.State.PENDING)
            .order_by("id")
            .first()
        )
        if node_command is None:
            return None

        node_command.state = NodeCommand.State.RUNNING
        node_command.claimed_at = now
        node_command.claimed_by_worker_instance = worker_instance_id
        try:
            # node_command_single_running refuses a second running command.
            with transaction.atomic():
                node_command.save(update_fields=["state", "claimed_at", "claimed_by_worker_instance"])
        except IntegrityError:
            return None
        return node_command


def record_node_command_progress(node_command_id: int, progress_step: ProgressStep) -> None:
    """Add the step to the command's progress, or replace the entry with the same step name."""
    with transaction.atomic():
        node_command = NodeCommand.objects.select_for_update().get(id=node_command_id)
        node_command.progress = replace_or_append_progress_step(node_command.progress, progress_step)
        node_command.save(update_fields=["progress"])


def replace_or_append_progress_step(
    stored_progress: list[dict[str, Any]], progress_step: ProgressStep
) -> list[dict[str, Any]]:
    updated_progress: list[dict[str, Any]] = []
    step_was_replaced = False
    for stored_step in stored_progress:
        if stored_step.get("step") == progress_step.step:
            updated_progress.append(progress_step.to_json())
            step_was_replaced = True
        else:
            updated_progress.append(stored_step)

    if not step_was_replaced:
        updated_progress.append(progress_step.to_json())
    return updated_progress


def finish_node_command(
    node_command_id: int,
    state: NodeCommand.State,
    now: datetime,
    result: Mapping[str, Any] | None = None,
    error_message: str = "",
) -> bool:
    """Compare-and-set a running command to a terminal state; False when it was not running.

    A failed or interrupted setup-run command also moves its run back, in the same transaction.
    """
    if state not in NodeCommand.TERMINAL_STATES:
        raise ValueError(f"{state} is not a terminal state of a node command.")

    with transaction.atomic():
        updated_row_count = NodeCommand.objects.filter(id=node_command_id, state=NodeCommand.State.RUNNING).update(
            state=state,
            finished_at=now,
            result=dict(result) if result is not None else None,
            error_message=error_message,
        )
        if updated_row_count == 0:
            return False

        if state in UNSUCCESSFUL_STATES:
            finished_command = NodeCommand.objects.get(id=node_command_id)
            if finished_command.setup_run_id is not None:
                setup_runs.fall_back_after_unsuccessful_command(finished_command, now)
    return True


def cancel_node_command(node_command_id: int, now: datetime) -> bool:
    """Compare-and-set a pending command to cancelled; False when the worker claimed it first."""
    updated_row_count = NodeCommand.objects.filter(id=node_command_id, state=NodeCommand.State.PENDING).update(
        state=NodeCommand.State.CANCELLED,
        finished_at=now,
    )
    return updated_row_count == 1


def interrupt_running_node_commands(now: datetime) -> list[NodeCommand]:
    """Start-up recovery: every command still running belongs to a dead process and becomes interrupted."""
    with transaction.atomic():
        interrupted_commands = list(
            NodeCommand.objects.select_for_update().filter(state=NodeCommand.State.RUNNING).order_by("id")
        )
        for interrupted_command in interrupted_commands:
            interrupted_command.state = NodeCommand.State.INTERRUPTED
            interrupted_command.finished_at = now
            interrupted_command.error_message = INTERRUPTED_COMMAND_ERROR_MESSAGE
            interrupted_command.save(update_fields=["state", "finished_at", "error_message"])
            if interrupted_command.setup_run_id is not None:
                setup_runs.fall_back_after_unsuccessful_command(interrupted_command, now)
    return interrupted_commands


def delete_old_node_commands(now: datetime) -> int:
    """Delete the commands created more than NODE_COMMAND_RETENTION_DAYS ago; returns how many."""
    deleted_count, _deleted_by_model = NodeCommand.objects.filter(
        created_at__lt=now - timedelta(days=NODE_COMMAND_RETENTION_DAYS)
    ).delete()
    return deleted_count


def is_node_command_terminal(node_command: NodeCommand) -> bool:
    return node_command.state in NodeCommand.TERMINAL_STATES
