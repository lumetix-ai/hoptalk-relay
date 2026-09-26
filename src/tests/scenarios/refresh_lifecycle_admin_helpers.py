"""Steps shared by the refresh, worker-lifecycle, operator and soak scenarios.

- Starting the relay with devices the operator added from their cards, and signed-in clients.
- `TriggeringLinkPolicy`: a link that drops, or acts on, particular direct messages by their text,
  so a scenario can lose exactly one "K" or switch a device off at exactly one moment.
- Killing the worker process in the middle of its work, and starting a new one on the same
  database and node.
- `NodePortGate`: the node's port, which a scenario can make disappear so the worker stays
  disconnected for as long as the scenario needs.
- `OperatorBrowser`: the operator's browser on the admin panel, each request served on a thread of
  its own with its own database connection, as the web process serves it next to the worker.
- Database reads that follow a message, its deliveries and the packets the relay sent.
"""

import asyncio
import contextlib
import errno
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from django.db import connections
from django.test import Client
from django.utils import timezone
from pytest_django import Settings

from directory.models import Contact, User
from hoptalk_relay.relay_settings import RelaySettings
from messaging.models import InboundDirectMessage, MessageDelivery, OutboundPacket, RefreshSession
from tests.panel.panel_client import get_page, get_partial, post_form, sign_in_test_client
from tests.panel_operator import PanelOperator
from tests.scenarios.scenario_setup import ClientStarter, add_device_from_its_card, every_contact_is_on_node, sign_in
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.fake_node_transport import FakeNodeConnector, FakeNodeUnavailableError
from tests.worker.fake_node.radio_packets import DirectMessagePacket, RadioPacket
from tests.worker.fake_node.simulated_mesh import LinkPolicy, SimulatedDevice, SimulatedMesh
from tests.worker.fake_node.waiting import wait_until
from tests.worker.relay_worker.worker_harness import (
    RelayWorkerHarness,
    configure_relay_node,
    wait_for_database,
)
from tests.worker.simulated_hoptalk_client import SimulatedHopTalkClient
from tests.worker.simulated_hoptalk_client_records import ConversationRefresh, OutgoingMessage, OutgoingMessageStatus
from worker.worker_state import RelayMode

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse as TestClientResponse

RADIO_LIKE_SUGGESTED_TIMEOUT_MILLISECONDS = 800
ACCEPTED_MESSAGE_STATUSES = frozenset(
    {OutgoingMessageStatus.SENT, OutgoingMessageStatus.DELIVERED, OutgoingMessageStatus.READ}
)
SINGLE_PART_TEXT_TEMPLATE = "Message {number} from {sender}"
# Ten parts of 104 bytes: the longest message the protocol allows.
TEN_PART_TEXT = "".join(f"part {part_number:02d} " + "x" * 96 for part_number in range(1, 11))

# The status an htmx poll answers when the page should stop polling.
HTMX_STOP_POLLING_STATUS = 286
PANEL_POLL_INTERVAL_SECONDS = 0.02
SETUP_STEP_TIMEOUT_SECONDS = 10.0
SETUP_CONFIGURATION_FORM = {
    "node_name": "HopTalk Relay",
    "radio_preset": "Australia (Narrow)",
    "frequency_megahertz": "916.575",
    "bandwidth_kilohertz": "62.5",
    "spreading_factor": "7",
    "coding_rate": "7",
    "path_hash_size": "2",
    "transmit_power_dbm": "20",
}


# ----- starting the relay ------------------------------------------------------------------------


async def start_relay_with_devices_from_cards(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    device_names: Iterable[str],
) -> dict[str, SimulatedDevice]:
    """A configured relay, running, with every device added from its card and put on the node."""
    await configure_relay_node(fake_companion_firmware)
    devices = {device_name: await add_device_from_its_card(simulated_mesh, device_name) for device_name in device_names}
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    await wait_for_database(every_contact_is_on_node, description="every device to be put on the relay's node")
    return devices


async def start_signed_in_client(
    start_client: ClientStarter, device: SimulatedDevice, username: str
) -> SimulatedHopTalkClient:
    client = start_client(device)
    await sign_in(client, username)
    return client


def give_up_on_devices_after_rounds(settings: Settings, round_count: int) -> None:
    """Retry deliveries and receipts only this many rounds, so a device that stays off is given up sooner."""
    relay_settings: RelaySettings = settings.RELAY_SETTINGS
    settings.RELAY_SETTINGS = replace(
        relay_settings, retry_strategy=replace(relay_settings.retry_strategy, maximum_attempts=round_count)
    )


def make_relay_node_suggest_radio_like_acknowledgement_waits(firmware: FakeCompanionFirmware) -> None:
    """The relay's node suggests ACK waits of most of a (scaled) second, as a real radio's airtime makes it do.

    The fake's own suggestions are a few milliseconds, which leaves no time to act while a packet
    still awaits its firmware ACK.
    """
    firmware.timing = replace(
        firmware.timing,
        flood_suggested_timeout_milliseconds=RADIO_LIKE_SUGGESTED_TIMEOUT_MILLISECONDS,
        direct_suggested_timeout_base_milliseconds=RADIO_LIKE_SUGGESTED_TIMEOUT_MILLISECONDS,
        direct_suggested_timeout_per_hop_milliseconds=0,
    )


def build_single_part_text(sender_username: str, number: int) -> str:
    return SINGLE_PART_TEXT_TEMPLATE.format(number=number, sender=sender_username)


async def wait_until_the_relay_accepted(message: OutgoingMessage) -> None:
    """Every part is confirmed; the message may be delivered already, which a wait for "sent" alone would miss."""
    await wait_until(
        lambda: message.status in ACCEPTED_MESSAGE_STATUSES,
        description=f"the relay to accept message {message.message_id}",
    )


async def wait_for_refresh_answer(client: SimulatedHopTalkClient, refresh: ConversationRefresh) -> int:
    """The count the server's "f" reported for the refresh."""
    await client.wait_until(
        lambda: refresh.reported_message_count is not None,
        description=f"the answer to F {refresh.refresh_target}",
    )
    assert refresh.reported_message_count is not None
    return refresh.reported_message_count


# ----- a link that acts on particular direct messages --------------------------------------------


@dataclass(kw_only=True)
class DirectMessageTrigger:
    """What the link does with the next direct messages whose text starts with `text_prefix`.

    `action` runs on the event loop right after the direct message left its node, so the message
    itself is already on its way when, for example, the action switches its sender off.
    """

    text_prefix: str
    remaining_matches: int = 1
    drops_the_message: bool = False
    action: Callable[[], None] | None = None
    matched_texts: list[str] = field(default_factory=list)

    @property
    def has_fired(self) -> bool:
        return bool(self.matched_texts)


@dataclass(kw_only=True)
class TriggeringLinkPolicy(LinkPolicy):
    """A link policy that first gives its triggers a look at every direct message it carries."""

    triggers: list[DirectMessageTrigger] = field(default_factory=list)

    def add_trigger(self, trigger: DirectMessageTrigger) -> DirectMessageTrigger:
        self.triggers.append(trigger)
        return trigger

    def loss_probability_for(self, packet: RadioPacket) -> float:
        if isinstance(packet, DirectMessagePacket):
            matching_trigger = self._take_matching_trigger(packet.text.decode("utf-8", "replace"))
            if matching_trigger is not None and matching_trigger.drops_the_message:
                return 1.0
        return super().loss_probability_for(packet)

    def _take_matching_trigger(self, text: str) -> DirectMessageTrigger | None:
        for trigger in self.triggers:
            if trigger.remaining_matches > 0 and text.startswith(trigger.text_prefix):
                trigger.remaining_matches -= 1
                trigger.matched_texts.append(text)
                if trigger.action is not None:
                    asyncio.get_running_loop().call_soon(trigger.action)
                return trigger
        return None


def install_triggering_links(device: SimulatedDevice) -> tuple[TriggeringLinkPolicy, TriggeringLinkPolicy]:
    """Replace both links of the device with triggering ones that otherwise deliver everything.

    Returns (uplink, downlink): the device's direct messages to the relay, and the relay's to it.
    """
    uplink = TriggeringLinkPolicy()
    downlink = TriggeringLinkPolicy()
    device.uplink = uplink
    device.downlink = downlink
    return uplink, downlink


# ----- the worker process ------------------------------------------------------------------------


async def kill_relay_worker(relay_worker: RelayWorkerHarness) -> None:
    """The worker process dies where it stands: no orderly shutdown, its tasks stop mid-step."""
    run_task = relay_worker.run_task
    assert run_task is not None, "the relay worker is not running"
    run_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await run_task
    relay_worker.forget_run_task()


def start_new_relay_worker_process(relay_worker: RelayWorkerHarness) -> None:
    """A new worker process on the same database and node, with nothing in memory."""
    relay_worker.worker = relay_worker.build_worker()
    relay_worker.start()


class NodePortGate:
    """The node's port as the worker's connector finds it; while closed, opening it fails as for an unplugged node."""

    def __init__(self, connector: FakeNodeConnector) -> None:
        self._connector = connector
        self.is_open = True
        self.refused_connection_attempts = 0

    async def __call__(self) -> Any:
        if not self.is_open:
            self.refused_connection_attempts += 1
            raise FakeNodeUnavailableError(errno.ENOENT, "could not open port: the node's port is gone")
        return await self._connector()

    def close(self) -> None:
        self.is_open = False

    def open(self) -> None:
        self.is_open = True


def put_node_port_gate_before_worker(
    relay_worker: RelayWorkerHarness, fake_node_connector: FakeNodeConnector
) -> NodePortGate:
    """Every worker process the harness starts from now on opens the node through the gate."""
    assert relay_worker.run_task is None, "the gate goes in before the worker starts"
    node_port_gate = NodePortGate(fake_node_connector)
    relay_worker.connector = node_port_gate
    relay_worker.worker = relay_worker.build_worker()
    return node_port_gate


# ----- the operator's browser --------------------------------------------------------------------


def serve_as_the_web_process[Result](handle_request: Callable[[], Result]) -> Result:
    """Run a request as the web process does: on its own thread, with its own database connection."""
    try:
        return handle_request()
    finally:
        connections.close_all()


class OperatorBrowser:
    """The operator signed in to the admin panel; every request runs concurrently with the worker."""

    def __init__(self, test_client: Client) -> None:
        self._test_client = test_client

    @classmethod
    async def sign_in(cls, panel_operator: PanelOperator) -> OperatorBrowser:
        test_client = await asyncio.to_thread(serve_as_the_web_process, lambda: sign_in_test_client(panel_operator))
        return cls(test_client)

    async def get_page(self, path: str) -> TestClientResponse:
        return await asyncio.to_thread(serve_as_the_web_process, lambda: get_page(self._test_client, path))

    async def get_partial(self, path: str) -> TestClientResponse:
        return await asyncio.to_thread(serve_as_the_web_process, lambda: get_partial(self._test_client, path))

    async def post_form(self, path: str, form_data: dict[str, str] | None = None) -> TestClientResponse:
        return await asyncio.to_thread(serve_as_the_web_process, lambda: post_form(self._test_client, path, form_data))


async def poll_partial_until_it_stops(
    browser: OperatorBrowser, path: str, *, timeout_seconds: float = SETUP_STEP_TIMEOUT_SECONDS
) -> TestClientResponse:
    """Poll a partial as htmx does until the server answers that polling should stop."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        response = await browser.get_partial(path)
        if response.status_code == HTMX_STOP_POLLING_STATUS:
            return response
        assert response.status_code == HTTPStatus.OK, response
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out after {timeout_seconds} s polling {path}")
        await asyncio.sleep(PANEL_POLL_INTERVAL_SECONDS)


async def run_setup_wizard(browser: OperatorBrowser, typed_node_name: str, *, keep_identity: bool = False) -> None:
    """Start setup, confirm the factory reset with the node's name, review and apply the configuration.

    Each step waits, as the wizard page does, until the worker has finished the step's node work.
    With keep_identity, the configuration keeps the relay's identity, which the form must offer.
    """
    start_response = await browser.post_form("/setup/start")
    assert start_response.status_code == HTTPStatus.FOUND
    await poll_partial_until_it_stops(browser, "/setup/partials/step")
    confirmation_page = (await browser.get_page("/setup")).content.decode()
    assert typed_node_name in confirmation_page

    reset_response = await browser.post_form("/setup/reset", {"typed_confirmation": typed_node_name})
    assert reset_response.status_code == HTTPStatus.FOUND
    await poll_partial_until_it_stops(browser, "/setup/partials/step")

    configuration_form = dict(SETUP_CONFIGURATION_FORM)
    if keep_identity:
        configuration_page = (await browser.get_page("/setup")).content.decode()
        assert 'type="checkbox" name="restore_identity"' in configuration_page
        configuration_form["restore_identity"] = "on"
    review_response = await browser.post_form("/setup/configure", {**configuration_form, "stage": "review"})
    assert review_response.status_code == HTTPStatus.OK
    assert "Apply and reboot" in review_response.content.decode()
    apply_response = await browser.post_form("/setup/configure", {**configuration_form, "stage": "apply"})
    assert apply_response.status_code == HTTPStatus.FOUND
    await poll_partial_until_it_stops(browser, "/setup/partials/step")


# ----- reading what the relay did ----------------------------------------------------------------


def read_delivery(sender_username: str, client_message_id: int, device: SimulatedDevice) -> MessageDelivery:
    return MessageDelivery.objects.select_related("message").get(
        message__sender__username_lookup=sender_username.lower(),
        message__client_message_id=client_message_id,
        device__public_key=device.public_key.hex(),
    )


def read_deliveries_to(device: SimulatedDevice) -> list[MessageDelivery]:
    return list(
        MessageDelivery.objects.select_related("message", "message__sender")
        .filter(device__public_key=device.public_key.hex())
        .order_by("message__accepted_at", "message_id")
    )


def read_delivery_packets(delivery_id: int) -> list[OutboundPacket]:
    return list(OutboundPacket.objects.filter(message_delivery_id=delivery_id).order_by("id"))


def read_packets_labelled_for(device: SimulatedDevice) -> list[OutboundPacket]:
    """Every packet the relay prepared for the device, also after its contact row was deleted."""
    return list(OutboundPacket.objects.filter(contact_label__contains=device.public_key.hex()[:12]).order_by("id"))


def read_refresh_sessions_of(device: SimulatedDevice) -> list[RefreshSession]:
    return list(
        RefreshSession.objects.select_related("peer").filter(device__public_key=device.public_key.hex()).order_by("id")
    )


def read_contact_of(device: SimulatedDevice) -> Contact | None:
    return Contact.objects.select_related("user").filter(public_key=device.public_key.hex()).first()


def read_user(username: str) -> User | None:
    return User.objects.filter(username_lookup=username.lower()).first()


def read_inbox_rows_from(device: SimulatedDevice) -> list[InboundDirectMessage]:
    return list(
        InboundDirectMessage.objects.filter(sender_public_key_prefix=device.public_key.hex()[:12]).order_by("id")
    )


def wall_clock_time_of(firmware_monotonic_time: float) -> datetime:
    """The wall-clock moment of a time the fake node took from time.monotonic()."""
    return timezone.now() + timedelta(seconds=firmware_monotonic_time - time.monotonic())


def find_packets_awaiting_acknowledgement_at(moment: datetime) -> list[OutboundPacket]:
    """Packets handed to the node before the moment whose firmware ACK was still awaited then."""
    return list(
        OutboundPacket.objects.filter(queued_at__lt=moment, acknowledgement_deadline_at__gt=moment)
        .exclude(acknowledged_at__lte=moment)
        .order_by("id")
    )


def find_first_packet_prepared_at(packets: list[OutboundPacket]) -> datetime:
    assert packets, "no packet was prepared"
    return min(packet.prepared_at for packet in packets if packet.prepared_at is not None)
