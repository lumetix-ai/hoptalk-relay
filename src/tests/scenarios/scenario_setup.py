"""The steps most scenarios start with: devices the operator added from their cards, and signed-in clients."""

from collections.abc import Callable

from django.utils import timezone

from directory.contacts import add_contact_from_card
from directory.models import Contact
from node.contact_cards import parse_contact_card_uri
from tests.worker.fake_node.simulated_mesh import SimulatedDevice, SimulatedMesh
from tests.worker.relay_worker.worker_harness import in_database
from tests.worker.simulated_hoptalk_client import SimulatedHopTalkClient
from tests.worker.simulated_hoptalk_client_records import ConversationRefresh

DEFAULT_PASSWORD = "correct horse battery"

# The start_client fixture: starts a reference client on a device.
type ClientStarter = Callable[[SimulatedDevice], SimulatedHopTalkClient]


async def add_device_from_its_card(simulated_mesh: SimulatedMesh, device_name: str) -> SimulatedDevice:
    """A device the relay's node does not know yet, added as the operator adds it: from its contact card.

    The worker's reconciler then puts it on the node; until then the node drops its direct messages.
    """
    device = simulated_mesh.add_device(device_name, relay_knows_device=False)
    contact_card = parse_contact_card_uri(device.contact_card_uri())
    await in_database(add_contact_from_card, contact_card, timezone.now())
    return device


def every_contact_is_on_node() -> bool:
    return not Contact.objects.exclude(node_sync_state=Contact.NodeSyncState.ON_NODE).exists()


async def sign_in(client: SimulatedHopTalkClient, username: str, password: str = DEFAULT_PASSWORD) -> None:
    """Sign in and wait until the "F *" every sign-in sends was answered too."""
    sign_in_request = client.sign_in(username, password)
    await sign_in_request.wait_until_finished()
    assert sign_in_request.is_signed_in, sign_in_request
    await client.wait_until(
        lambda: not any(isinstance(request, ConversationRefresh) for request in client.unfinished_requests),
        description=f"the refresh after {username} signed in to be answered",
    )
