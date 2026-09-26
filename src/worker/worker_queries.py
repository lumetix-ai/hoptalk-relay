"""Small reads the worker's tasks need besides the services."""

from uuid import UUID

from directory.models import Contact
from node.models import NodeCommand, NodeSetupRun


def read_contact_public_key(contact_id: int) -> str | None:
    return Contact.objects.filter(id=contact_id).values_list("public_key", flat=True).first()


def read_existing_contact_ids() -> set[int]:
    return set(Contact.objects.values_list("id", flat=True))


def read_setup_run(setup_run_id: int) -> NodeSetupRun | None:
    return NodeSetupRun.objects.filter(id=setup_run_id).first()


def read_node_command_ids_left_running_by(worker_instance_id: UUID) -> list[int]:
    running_commands = NodeCommand.objects.filter(
        state=NodeCommand.State.RUNNING, claimed_by_worker_instance=worker_instance_id
    )
    return list(running_commands.order_by("id").values_list("id", flat=True))
