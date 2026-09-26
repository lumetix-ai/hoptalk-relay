"""Shared steps of the account and message scenarios.

- Starting the relay with devices the operator added from their cards.
- Losing chosen direct messages on the mesh: a rule picks them by their text, so a scenario can lose
  "the answer to the first sign-in" or "part 2 of this message" instead of a random share of traffic.
- The stock MeshCore app, for people who type protocol lines by hand (docs/protocol.md, Appendix A).
- A phone whose wall clock is behind, and what a client asked and sent.
- Moving the worker's clock and reading what the relay stored.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any

from directory.models import Contact, User
from messaging.models import InboundDirectMessage, Message, MessageDelivery, OutboundPacket, ReceiptNotification
from protocol.constants import REFRESH_ALL_PEERS_TARGET
from protocol.usernames import normalize_username_for_lookup
from tests.scenarios.scenario_setup import add_device_from_its_card, every_contact_is_on_node
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware, ReceptionOutcome, TextMessageQueued
from tests.worker.fake_node.frames import PUBLIC_KEY_PREFIX_BYTES, TextType
from tests.worker.fake_node.radio_packets import DirectMessagePacket, RadioPacket
from tests.worker.fake_node.simulated_mesh import DeliveryOutcome, LinkPolicy, SimulatedDevice, SimulatedMesh
from tests.worker.fake_node.waiting import wait_until
from tests.worker.relay_worker.worker_harness import RelayWorkerHarness, configure_relay_node, wait_for_database
from tests.worker.simulated_hoptalk_client import SimulatedHopTalkClient
from tests.worker.simulated_hoptalk_client_node_link import SentDirectMessage
from tests.worker.simulated_hoptalk_client_records import ConversationRefresh, RequestState
from tests.worker.simulated_hoptalk_client_timing import ClientClock
from worker.worker_state import RelayMode

CERTAIN_LOSS_PROBABILITY = 1.0
MILLISECONDS_PER_SECOND = 1000
# The stock app waits for a firmware ACK as long as its node suggests, times this factor, before it resends.
STOCK_APP_ACKNOWLEDGEMENT_WAIT_FACTOR = 1.2


async def start_relay_with_devices(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    *device_names: str,
) -> list[SimulatedDevice]:
    """The configured relay node, a device per name added from its card, and the worker running with all on the node."""
    await configure_relay_node(fake_companion_firmware)
    devices = [await add_device_from_its_card(simulated_mesh, device_name) for device_name in device_names]
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    await wait_for_database(every_contact_is_on_node, description="every device to be put on the relay's node")
    return devices


# ----- losing chosen direct messages ----------------------------------------------------------


def always_applies() -> bool:
    return True


def text_is(expected_text: str) -> Callable[[str], bool]:
    return lambda text: text == expected_text


def text_starts_with(prefix: str) -> Callable[[str], bool]:
    return lambda text: text.startswith(prefix)


@dataclass(kw_only=True, eq=False)
class DirectMessageLossRule:
    """Loses every direct message whose text it matches while `applies` holds, at most `maximum_losses` of them."""

    matches_text: Callable[[str], bool]
    applies: Callable[[], bool] = always_applies
    maximum_losses: int | None = None
    lost_texts: list[str] = field(default_factory=list)

    def loses(self, text: str) -> bool:
        if not self.matches_text(text) or not self.applies():
            return False
        if self.maximum_losses is not None and len(self.lost_texts) >= self.maximum_losses:
            return False
        self.lost_texts.append(text)
        return True


@dataclass(kw_only=True)
class LinkPolicyWithLossRules(LinkPolicy):
    """A link policy that, on top of its random behaviour, loses every direct message one of its rules picks."""

    loss_rules: list[DirectMessageLossRule] = field(default_factory=list)

    def loss_probability_for(self, packet: RadioPacket) -> float:
        if isinstance(packet, DirectMessagePacket):
            text = packet.text.decode("utf-8", "replace")
            # Every rule sees the packet, so that each one counts the copies it would have lost.
            losing_rules = [rule for rule in self.loss_rules if rule.loses(text)]
            if losing_rules:
                return CERTAIN_LOSS_PROBABILITY
        return super().loss_probability_for(packet)


@dataclass(kw_only=True, eq=False)
class LossPeriod:
    """A stretch of a scenario during which loss rules apply: pass `lasts` as a rule's `applies`, then call `end`."""

    is_over: bool = False

    def lasts(self) -> bool:
        return not self.is_over

    def end(self) -> None:
        self.is_over = True


def with_loss_rule(link_policy: LinkPolicy, loss_rule: DirectMessageLossRule) -> LinkPolicyWithLossRules:
    if not isinstance(link_policy, LinkPolicyWithLossRules):
        policy_values: dict[str, Any] = {
            policy_field.name: getattr(link_policy, policy_field.name) for policy_field in fields(LinkPolicy)
        }
        link_policy = LinkPolicyWithLossRules(**policy_values)
    link_policy.loss_rules.append(loss_rule)
    return link_policy


def lose_direct_messages_to(
    device: SimulatedDevice,
    matches_text: Callable[[str], bool],
    *,
    applies: Callable[[], bool] = always_applies,
    maximum_losses: int | None = None,
) -> DirectMessageLossRule:
    """The relay's direct messages to the device that the rule picks are lost on the way (with their firmware ACKs)."""
    loss_rule = DirectMessageLossRule(matches_text=matches_text, applies=applies, maximum_losses=maximum_losses)
    device.downlink = with_loss_rule(device.downlink, loss_rule)
    return loss_rule


def lose_direct_messages_from(
    device: SimulatedDevice,
    matches_text: Callable[[str], bool],
    *,
    applies: Callable[[], bool] = always_applies,
    maximum_losses: int | None = None,
) -> DirectMessageLossRule:
    """The device's direct messages to the relay that the rule picks are lost on the way."""
    loss_rule = DirectMessageLossRule(matches_text=matches_text, applies=applies, maximum_losses=maximum_losses)
    device.uplink = with_loss_rule(device.uplink, loss_rule)
    return loss_rule


def count_direct_messages_the_relay_node_took(
    simulated_mesh: SimulatedMesh, device: SimulatedDevice, text_prefix: str
) -> int:
    """How many direct messages starting with the prefix reached the relay's node from the device and were taken."""
    relay_label = simulated_mesh.relay_firmware.label
    taken_texts = [
        record.packet.text.decode("utf-8", "replace")
        for record in simulated_mesh.traffic(sender=device.name, recipient=relay_label, packet_type=DirectMessagePacket)
        if isinstance(record.packet, DirectMessagePacket)
        and record.outcome is DeliveryOutcome.DELIVERED
        and record.reception is ReceptionOutcome.ACCEPTED
    ]
    return sum(1 for text in taken_texts if text.startswith(text_prefix))


def until_the_relay_node_took(
    simulated_mesh: SimulatedMesh, device: SimulatedDevice, text_prefix: str, *, count: int
) -> Callable[[], bool]:
    """For a loss rule: holds until `count` direct messages starting with the prefix reached the relay's node.

    Losing an answer until the client's retry has reached the relay makes sure that the answer the
    client finally gets was sent after the relay had the retry, whatever the relay resent on its own.
    """
    return lambda: count_direct_messages_the_relay_node_took(simulated_mesh, device, text_prefix) < count


# ----- the stock MeshCore app ------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class TypedLine:
    """One direct message the stock app handed to its node."""

    text: str
    sender_timestamp: int
    attempt: int
    expected_acknowledgement: bytes
    suggested_timeout_milliseconds: int


class StockMeshCoreApp:
    """The stock MeshCore app on a device: a person types protocol lines to the relay by hand and reads the answers.

    It follows none of a HopTalk client's rules: no retries of requests, no coalescing, no pacing
    beyond the person's typing. Like the real app, it gives every typed line a new MeshCore
    timestamp, and it resends a line whose firmware ACK does not come back as a firmware-level
    repeat: the same timestamp with the next attempt number.
    """

    def __init__(self, device: SimulatedDevice, *, resends_without_acknowledgement: int = 0) -> None:
        self.device = device
        self.resends_without_acknowledgement = resends_without_acknowledgement
        self.typed_lines: list[TypedLine] = []
        self.received_texts: list[str] = []
        self._last_sender_timestamp = 0

    def type_line(self, text: str) -> TypedLine:
        sender_timestamp = max(int(time.time()), self._last_sender_timestamp + 1)
        self._last_sender_timestamp = sender_timestamp
        return self._hand_to_node(text, sender_timestamp=sender_timestamp, attempt=0)

    def repeat_at_firmware_level(self, typed_line: TypedLine) -> TypedLine:
        """The app's own resend: the same text and timestamp with the next attempt number."""
        return self._hand_to_node(
            typed_line.text, sender_timestamp=typed_line.sender_timestamp, attempt=typed_line.attempt + 1
        )

    async def type_line_and_resend_while_unacknowledged(self, text: str) -> list[TypedLine]:
        """Type a line; while its firmware ACK does not come back in time, resend it as a firmware-level repeat."""
        copies = [self.type_line(text)]
        while len(copies) <= self.resends_without_acknowledgement:
            latest_copy = copies[-1]
            acknowledgement_wait_seconds = (
                STOCK_APP_ACKNOWLEDGEMENT_WAIT_FACTOR
                * latest_copy.suggested_timeout_milliseconds
                / MILLISECONDS_PER_SECOND
            )
            acknowledgement = await self.device.wait_for_acknowledgement(
                latest_copy.expected_acknowledgement, timeout_seconds=acknowledgement_wait_seconds
            )
            if acknowledgement is not None:
                break
            copies.append(self.repeat_at_firmware_level(latest_copy))
        return copies

    def read_new_lines(self) -> None:
        relay_public_key_prefix = self.device.relay_public_key[:PUBLIC_KEY_PREFIX_BYTES]
        self.received_texts.extend(
            received_direct_message.text
            for received_direct_message in self.device.receive_direct_messages()
            if received_direct_message.sender_public_key_prefix == relay_public_key_prefix
            and received_direct_message.text_type == TextType.PLAIN
        )

    def count_received(self, expected_text: str) -> int:
        self.read_new_lines()
        return self.received_texts.count(expected_text)

    async def wait_for_line(self, expected_text: str, *, copies: int = 1, timeout_seconds: float = 5.0) -> None:
        await wait_until(
            lambda: self.count_received(expected_text) >= copies,
            timeout_seconds=timeout_seconds,
            description=f"{self.device.name} to receive {expected_text!r} {copies} times, not {self.received_texts}",
        )

    def _hand_to_node(self, text: str, *, sender_timestamp: int, attempt: int) -> TypedLine:
        send_result = self.device.send_direct_message(text, sender_timestamp=sender_timestamp, attempt=attempt)
        assert isinstance(send_result, TextMessageQueued), f"{self.device.name} refused {text!r}: {send_result!r}"
        typed_line = TypedLine(
            text=text,
            sender_timestamp=sender_timestamp,
            attempt=attempt,
            expected_acknowledgement=send_result.expected_acknowledgement,
            suggested_timeout_milliseconds=send_result.suggested_timeout_milliseconds,
        )
        self.typed_lines.append(typed_line)
        return typed_line


def deliver_delayed_retry(device: SimulatedDevice, text: str) -> None:
    """A retry of `text` that the mesh held back reaches the relay now, as a direct message of its own."""
    send_result = device.send_direct_message(text)
    assert isinstance(send_result, TextMessageQueued), f"{device.name} refused {text!r}: {send_result!r}"


# ----- clients -----------------------------------------------------------------------------------


class WallClockBehind:
    """A phone whose wall clock is behind the others' by a fixed amount; its monotonic time runs as theirs."""

    def __init__(self, clock: ClientClock, *, seconds_behind: float) -> None:
        self._clock = clock
        self._seconds_behind = seconds_behind

    def monotonic_seconds(self) -> float:
        return self._clock.monotonic_seconds()

    def wall_clock_seconds(self) -> float:
        return self._clock.wall_clock_seconds() - self._seconds_behind


def find_refreshes_of_every_conversation(client: SimulatedHopTalkClient) -> list[ConversationRefresh]:
    """Every "F *" the client made, oldest first."""
    requests = [*client.storage.finished_requests, *client.storage.unfinished_requests]
    refreshes = [
        request for request in requests if isinstance(request, ConversationRefresh) and request.is_for_all_peers
    ]
    return sorted(refreshes, key=lambda refresh: refresh.created_at)


async def wait_for_answered_refresh_of_every_conversation(client: SimulatedHopTalkClient) -> ConversationRefresh:
    """Wait until the latest "F *" of the client got its "f *"; return it."""

    def latest_refresh_is_answered() -> bool:
        refreshes = find_refreshes_of_every_conversation(client)
        return bool(refreshes) and refreshes[-1].state is RequestState.ANSWERED

    await client.wait_until(
        latest_refresh_is_answered, description=f"{client!r} to get its f {REFRESH_ALL_PEERS_TARGET}"
    )
    return find_refreshes_of_every_conversation(client)[-1]


def find_sent_direct_messages(client: SimulatedHopTalkClient, text_prefix: str) -> list[SentDirectMessage]:
    return [
        sent_direct_message
        for sent_direct_message in client.sent_direct_messages
        if sent_direct_message.text.startswith(text_prefix)
    ]


# ----- the worker's clock ----------------------------------------------------------------------


def read_worker_time(relay_worker: RelayWorkerHarness) -> datetime:
    return relay_worker.clock.now()


def move_worker_clock_to(relay_worker: RelayWorkerHarness, moment: datetime) -> None:
    """Time passes for the worker at once: every wait it had until `moment` ends."""
    seconds_to_move = (moment - read_worker_time(relay_worker)).total_seconds()
    assert seconds_to_move > 0, f"The worker's clock is already past {moment}."
    relay_worker.clock.advance(seconds=seconds_to_move)


async def wait_until_worker_time_passes(
    relay_worker: RelayWorkerHarness, moment: datetime, *, timeout_seconds: float = 10.0
) -> None:
    await wait_until(
        lambda: read_worker_time(relay_worker) > moment,
        timeout_seconds=timeout_seconds,
        description=f"the worker's clock to pass {moment}",
    )


# ----- what the relay stored (run these with in_database) ----------------------------------------


def read_contact(device: SimulatedDevice) -> Contact:
    return Contact.objects.get(public_key=device.public_key.hex())


def read_user(username: str) -> User:
    return User.objects.get(username_lookup=normalize_username_for_lookup(username))


def read_inbox_rows_from(device: SimulatedDevice) -> list[InboundDirectMessage]:
    return list(InboundDirectMessage.objects.filter(contact__public_key=device.public_key.hex()).order_by("id"))


def count_recorded_direct_messages_from(device: SimulatedDevice) -> int:
    """Inbox rows from the device, plus the firmware-level repeats counted on them."""
    return sum(1 + inbox_row.duplicate_count for inbox_row in read_inbox_rows_from(device))


async def wait_until_the_relay_recorded_everything_from(simulated_mesh: SimulatedMesh, device: SimulatedDevice) -> None:
    """Wait until the worker recorded every direct message the relay's node took from the device."""
    await simulated_mesh.wait_until_idle()
    taken_count = count_direct_messages_the_relay_node_took(simulated_mesh, device, "")
    await wait_for_database(
        lambda: count_recorded_direct_messages_from(device) >= taken_count,
        description=f"the relay to record the {taken_count} direct messages its node took from {device.name}",
    )


def count_processed_inbox_rows_from(device: SimulatedDevice, text_prefix: str) -> int:
    return InboundDirectMessage.objects.filter(
        contact__public_key=device.public_key.hex(),
        text__startswith=text_prefix,
        processing_state=InboundDirectMessage.ProcessingState.PROCESSED,
    ).count()


def read_packets_to(device: SimulatedDevice) -> list[OutboundPacket]:
    return list(OutboundPacket.objects.filter(contact__public_key=device.public_key.hex()).order_by("id"))


def read_message(sender_username: str, client_message_id: int) -> Message | None:
    return Message.objects.filter(
        sender__username_lookup=normalize_username_for_lookup(sender_username), client_message_id=client_message_id
    ).first()


def read_messages_sent_by(sender_username: str) -> list[Message]:
    return list(
        Message.objects.filter(sender__username_lookup=normalize_username_for_lookup(sender_username)).order_by("id")
    )


def read_delivery(sender_username: str, client_message_id: int, device: SimulatedDevice) -> MessageDelivery:
    return MessageDelivery.objects.get(
        message__sender__username_lookup=normalize_username_for_lookup(sender_username),
        message__client_message_id=client_message_id,
        device__public_key=device.public_key.hex(),
    )


def find_receipt(sender_username: str, client_message_id: int, device: SimulatedDevice) -> ReceiptNotification | None:
    return ReceiptNotification.objects.filter(
        message__sender__username_lookup=normalize_username_for_lookup(sender_username),
        message__client_message_id=client_message_id,
        device__public_key=device.public_key.hex(),
    ).first()


def read_receipt(sender_username: str, client_message_id: int, device: SimulatedDevice) -> ReceiptNotification:
    receipt = find_receipt(sender_username, client_message_id, device)
    assert receipt is not None, f"No receipt of message {client_message_id} of {sender_username} to {device.name}."
    return receipt


def has_receipt_sent_at_least_once(sender_username: str, client_message_id: int, device: SimulatedDevice) -> bool:
    receipt = find_receipt(sender_username, client_message_id, device)
    return receipt is not None and receipt.attempt_count >= 1


def has_receipt_in_state(
    sender_username: str,
    client_message_id: int,
    device: SimulatedDevice,
    state: ReceiptNotification.State,
    *,
    confirmed_level: int | None = None,
) -> bool:
    receipt = find_receipt(sender_username, client_message_id, device)
    if receipt is None or receipt.state != state:
        return False
    return confirmed_level is None or receipt.confirmed_level == confirmed_level


def read_packets_of_delivery(delivery_id: int) -> list[OutboundPacket]:
    return list(OutboundPacket.objects.filter(message_delivery_id=delivery_id).order_by("id"))


def read_packets_of_receipt(receipt_id: int) -> list[OutboundPacket]:
    return list(OutboundPacket.objects.filter(receipt_notification_id=receipt_id).order_by("id"))
