"""assert_all_invariants(): every invariant of the delivery engine that the database alone can show, over every row.

Service and scenario tests call it at the end. Each check returns the violations it found as
readable sentences, so a failure names every broken rule at once.
"""

from collections import Counter, defaultdict
from datetime import timedelta
from itertools import pairwise

from directory.contacts import CONTACT_CAPACITY
from directory.models import Contact
from hoptalk_relay.relay_settings import get_relay_settings
from messaging.models import (
    InboundDirectMessage,
    Message,
    MessageDelivery,
    OutboundPacket,
    ReceiptNotification,
    RefreshSession,
)
from node.models import NodeSetting
from node.node_identity_backups import NodeIdentityBackupStatus, read_node_identity_backup_state
from node.node_settings import IncompleteNodeConfigurationError, NodeSettingKey, load_node_configuration
from protocol.constants import FIRMWARE_REPEAT_WINDOW_HOURS
from protocol.received_sets import calculate_all_parts_mask

NON_TERMINAL_DELIVERY_STATES = (
    MessageDelivery.State.PENDING,
    MessageDelivery.State.QUEUED_FOR_REFRESH,
    MessageDelivery.State.FAILED,
)
OUTSTANDING_DELIVERY_STATES = (MessageDelivery.State.PENDING, MessageDelivery.State.QUEUED_FOR_REFRESH)
READ_LEVEL = ReceiptNotification.TargetLevel.READ


def assert_all_invariants() -> None:
    violations = [
        *find_account_violations(),
        *find_contact_violations(),
        *find_message_violations(),
        *find_delivery_violations(),
        *find_receipt_violations(),
        *find_refresh_session_violations(),
        *find_packet_violations(),
        *find_node_setting_violations(),
        *find_inbox_violations(),
    ]
    assert not violations, "Broken invariants:\n" + "\n".join(f"- {violation}" for violation in violations)


def find_account_violations() -> list[str]:
    """A device belongs to at most one user, and its live deliveries and receipts concern that user."""
    violations: list[str] = []
    device_user_ids = dict(Contact.objects.values_list("id", "user_id"))

    live_deliveries = MessageDelivery.objects.filter(state__in=NON_TERMINAL_DELIVERY_STATES).values_list(
        "id", "device_id", "message__recipient_id"
    )
    for delivery_id, device_id, recipient_id in live_deliveries:
        if device_user_ids.get(device_id) != recipient_id:
            violations.append(f"Delivery {delivery_id} is live, but its device is not linked to the recipient.")

    live_receipts = (
        ReceiptNotification.objects.exclude(state=ReceiptNotification.State.CANCELLED)
        .exclude(state=ReceiptNotification.State.CONFIRMED, confirmed_level=READ_LEVEL)
        .values_list("id", "device_id", "message__sender_id")
    )
    for receipt_id, device_id, sender_id in live_receipts:
        if device_user_ids.get(device_id) != sender_id:
            violations.append(f"Receipt {receipt_id} is live, but its device is not linked to the message's sender.")
    return violations


def find_contact_violations() -> list[str]:
    violations: list[str] = []
    public_keys = list(Contact.objects.values_list("public_key", flat=True))
    if len(set(public_keys)) != len(public_keys):
        violations.append("Two contacts share a public key.")
    if len({public_key[:12] for public_key in public_keys}) != len(public_keys):
        violations.append("Two contacts share a six-byte key prefix.")
    if len(public_keys) > CONTACT_CAPACITY:
        violations.append(f"There are {len(public_keys)} contacts, more than {CONTACT_CAPACITY}.")

    relay_public_key = (
        NodeSetting.objects.filter(key=NodeSettingKey.NODE_PUBLIC_KEY.value).values_list("value", flat=True).first()
    )
    if relay_public_key and relay_public_key in public_keys:
        violations.append("The relay node's own key is a contact.")
    return violations


def find_message_violations() -> list[str]:
    violations: list[str] = []
    client_ids = Counter(Message.objects.values_list("sender_id", "client_message_id"))
    violations.extend(
        f"Sender {sender_id} has {count} messages with id {client_message_id}."
        for (sender_id, client_message_id), count in client_ids.items()
        if count > 1
    )

    message_ids_with_deliveries = set(MessageDelivery.objects.values_list("message_id", flat=True))
    message_ids_with_receipts = set(ReceiptNotification.objects.values_list("message_id", flat=True))
    message_ids_delivered_to_a_device = set(
        MessageDelivery.objects.filter(state=MessageDelivery.State.DELIVERED).values_list("message_id", flat=True)
    )
    for message in Message.objects.all():
        violations.extend(find_single_message_violations(message))
        if message.accepted_at is None and message.pk in message_ids_with_deliveries | message_ids_with_receipts:
            violations.append(f"Message {message.pk} is incomplete but has deliveries or receipts.")
        if message.pk in message_ids_delivered_to_a_device and message.delivered_at is None:
            violations.append(f"Message {message.pk} was delivered to a device but has no delivered_at.")
    return violations


def find_single_message_violations(message: Message) -> list[str]:
    violations: list[str] = []
    held_parts = [part_text for part_text in message.part_texts if part_text is not None]
    if message.accepted_at is not None:
        if len(held_parts) != message.part_count:
            violations.append(f"Message {message.pk} is accepted without every part.")
        if message.text != "".join(held_parts):
            violations.append(f"Message {message.pk} has a text that is not its parts in order.")
    if message.read_at is not None and message.delivered_at is None:
        violations.append(f"Message {message.pk} was read but never delivered.")
    if message.delivered_at is not None and message.accepted_at is None:
        violations.append(f"Message {message.pk} was delivered but never accepted.")
    return violations


def find_delivery_violations() -> list[str]:
    violations: list[str] = []
    per_device_pairs = Counter(MessageDelivery.objects.values_list("message_id", "device_id"))
    violations.extend(
        f"Message {message_id} has {count} deliveries to device {device_id}."
        for (message_id, device_id), count in per_device_pairs.items()
        if count > 1
    )

    maximum_active_deliveries = get_relay_settings().pacing.maximum_active_deliveries_per_device
    in_progress_counts = Counter(
        MessageDelivery.objects.filter(state=MessageDelivery.State.PENDING, round_started_at__isnull=False).values_list(
            "device_id", flat=True
        )
    )
    violations.extend(
        f"Device {device_id} has {count} deliveries in progress, more than {maximum_active_deliveries}."
        for device_id, count in in_progress_counts.items()
        if count > maximum_active_deliveries
    )

    for delivery in MessageDelivery.objects.select_related("message"):
        all_parts_mask = calculate_all_parts_mask(delivery.message.part_count)
        if delivery.attempt_count > delivery.maximum_attempts:
            violations.append(f"Delivery {delivery.pk} started more rounds than its maximum.")
        if delivery.state == MessageDelivery.State.DELIVERED and delivery.delivered_at is None:
            violations.append(f"Delivery {delivery.pk} is delivered without delivered_at.")
        if delivery.round_pending_parts_mask & ~all_parts_mask or delivery.parts_received_mask & ~all_parts_mask:
            violations.append(f"Delivery {delivery.pk} has a mask bit beyond its message's part count.")
        if delivery.state != MessageDelivery.State.DELIVERED and delivery.parts_received_mask == all_parts_mask:
            violations.append(f"Delivery {delivery.pk} holds a complete set but is {delivery.state}.")
    return violations


def find_receipt_violations() -> list[str]:
    """Levels only grow, the confirmed level never passes the target, and the target never passes the message."""
    violations: list[str] = []
    for receipt in ReceiptNotification.objects.select_related("message"):
        message = receipt.message
        message_level = 2 if message.read_at is not None else 1 if message.delivered_at is not None else 0
        if not receipt.confirmed_level <= receipt.target_level <= message_level:
            violations.append(
                f"Receipt {receipt.pk} has levels {receipt.confirmed_level} of {receipt.target_level} "
                f"for a message at level {message_level}."
            )
        is_confirmed_state = receipt.state == ReceiptNotification.State.CONFIRMED
        levels_are_equal = receipt.confirmed_level == receipt.target_level
        if receipt.state != ReceiptNotification.State.CANCELLED and is_confirmed_state != levels_are_equal:
            violations.append(
                f"Receipt {receipt.pk} is {receipt.state} "
                f"with levels {receipt.confirmed_level} of {receipt.target_level}."
            )
        if receipt.attempt_count > receipt.maximum_attempts:
            violations.append(f"Receipt {receipt.pk} made more attempts than its maximum.")
    return violations


def find_refresh_session_violations() -> list[str]:
    violations: list[str] = []
    active_pairs = Counter(
        RefreshSession.objects.filter(state=RefreshSession.State.ACTIVE).values_list("device_id", "peer_id")
    )
    violations.extend(
        f"Device {device_id} has {count} active refresh sessions with peer {peer_id}."
        for (device_id, peer_id), count in active_pairs.items()
        if count > 1
    )

    owned_deliveries_by_session_id: dict[int, list[MessageDelivery]] = defaultdict(list)
    for delivery in MessageDelivery.objects.filter(refresh_session__isnull=False).select_related("message"):
        if delivery.refresh_session_id is not None:
            owned_deliveries_by_session_id[delivery.refresh_session_id].append(delivery)

    for refresh_session in RefreshSession.objects.all():
        owned_deliveries = owned_deliveries_by_session_id[refresh_session.pk]
        if refresh_session.state == RefreshSession.State.ACTIVE:
            violations.extend(find_active_session_violations(refresh_session, owned_deliveries))
        elif any(delivery.state in OUTSTANDING_DELIVERY_STATES for delivery in owned_deliveries):
            violations.append(f"Refresh session {refresh_session.pk} is {refresh_session.state} but still owns work.")
    return violations


def find_active_session_violations(
    refresh_session: RefreshSession, owned_deliveries: list[MessageDelivery]
) -> list[str]:
    """Exactly one head, which comes before every queued message in acceptance order."""
    violations: list[str] = []
    heads = [delivery for delivery in owned_deliveries if delivery.state == MessageDelivery.State.PENDING]
    queued_deliveries = [
        delivery for delivery in owned_deliveries if delivery.state == MessageDelivery.State.QUEUED_FOR_REFRESH
    ]
    if len(heads) != 1:
        violations.append(f"Active refresh session {refresh_session.pk} has {len(heads)} heads.")
    for head in heads:
        head_order = (head.message.accepted_at, head.message_id)
        for queued_delivery in queued_deliveries:
            if (queued_delivery.message.accepted_at, queued_delivery.message_id) < head_order:
                violations.append(
                    f"Active refresh session {refresh_session.pk} queues message {queued_delivery.message_id} "
                    f"before its head's message {head.message_id}."
                )
    for owned_delivery in queued_deliveries + heads:
        is_session_conversation = (
            owned_delivery.device_id == refresh_session.device_id
            and owned_delivery.message.sender_id == refresh_session.peer_id
        )
        if not is_session_conversation:
            violations.append(f"Delivery {owned_delivery.pk} is owned by a session of another conversation.")
    return violations


def find_packet_violations() -> list[str]:
    """Every packet has a MeshCore timestamp larger than every earlier packet's."""
    violations: list[str] = []
    previous_timestamp: int | None = None
    for packet_id, sender_timestamp in OutboundPacket.objects.order_by("id").values_list("id", "sender_timestamp"):
        if previous_timestamp is not None and sender_timestamp <= previous_timestamp:
            violations.append(f"Packet {packet_id} has a timestamp that does not increase.")
        previous_timestamp = sender_timestamp
    return violations


def find_node_setting_violations() -> list[str]:
    """node_setting is empty or complete, and an identity backup belongs to the configured node."""
    try:
        node_configuration = load_node_configuration()
    except IncompleteNodeConfigurationError as incomplete_configuration:
        return [str(incomplete_configuration)]

    backup_state = read_node_identity_backup_state()
    if backup_state.status == NodeIdentityBackupStatus.ABSENT or not backup_state.public_key:
        return []
    if node_configuration is None:
        return ["An identity backup is stored although no node is configured."]
    if backup_state.public_key != node_configuration.node_public_key:
        return [
            f"The identity backup belongs to key {backup_state.public_key[:12]}, "
            f"not to the configured node {node_configuration.node_public_key[:12]}."
        ]
    return []


def find_inbox_violations() -> list[str]:
    """A firmware-level repeat is counted on its original row, never recorded as a row of its own."""
    violations: list[str] = []
    rows_by_repeat_key: dict[tuple[int, int, str], list[InboundDirectMessage]] = defaultdict(list)
    for inbox_row in InboundDirectMessage.objects.filter(contact__isnull=False).order_by("id"):
        if inbox_row.contact_id is not None:
            rows_by_repeat_key[(inbox_row.contact_id, inbox_row.sender_timestamp, inbox_row.text_sha256)].append(
                inbox_row
            )

    repeat_window = timedelta(hours=FIRMWARE_REPEAT_WINDOW_HOURS)
    for inbox_rows in rows_by_repeat_key.values():
        for earlier_row, later_row in pairwise(inbox_rows):
            if later_row.received_at - earlier_row.received_at < repeat_window:
                violations.append(f"Inbox row {later_row.pk} is a firmware-level repeat of row {earlier_row.pk}.")
    return violations
