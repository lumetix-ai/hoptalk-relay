"""Refresh sessions: re-delivering a peer's missed messages to one device, oldest first, one message at a time.

"F <peer>" and "F *" start one session per peer whose scope is not empty. The scope is the
device's failed, relink-cancelled and not yet owned pending deliveries of the peer's messages
to the user, plus the peer's messages that no device of the user received and that have no row
for this device. The session owns those deliveries; exactly one of them, the head, is pending
at a time, in acceptance order (accepted_at, id). A delivered head advances the session to its
next message; an exhausted head stops it and fails the rest unsent, so the device never gets a
gap. A repeated "F" restarts the head with fresh counters.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Q, QuerySet

from directory.contacts import LockedRowChangedError
from directory.models import Contact, User
from messaging.deliveries import (
    arm_refresh_session_head,
    create_refresh_owned_delivery,
    fail_delivery_of_stopped_refresh,
    restart_refresh_session_head,
    take_delivery_into_refresh_session,
)
from messaging.models import Message, MessageDelivery, RefreshSession
from messaging.receipts import rearm_receipts_for_refresh
from messaging.service_transactions import run_in_service_transaction
from protocol.constants import ErrorCode
from protocol.error_replies import build_request_error_reply
from protocol.message_types import ErrorReply, RefreshReply, RefreshRequest
from protocol.usernames import normalize_username_for_lookup

logger = logging.getLogger(__name__)

OUTSTANDING_DELIVERY_STATES = (MessageDelivery.State.PENDING, MessageDelivery.State.QUEUED_FOR_REFRESH)


@dataclass(frozen=True, kw_only=True)
class RefreshRequestOutcome:
    reply: RefreshReply | ErrorReply
    outcome_summary: str


@dataclass(frozen=True, kw_only=True)
class PeerRefresh:
    """What one "F" did for one peer."""

    message_count: int
    started_new_session: bool


def start_refresh(
    device: Contact,
    request: RefreshRequest,
    now: datetime,
    requested_by_inbound_id: int | None = None,
) -> RefreshRequestOutcome:
    """Start or restart the refresh of one peer's conversation, or of every conversation for "*".

    Answers "f <peer> <count>" or "f * <total>" with the number of messages that will follow, or
    "e NOT_SIGNED_IN / NO_SUCH_USER / SELF F <peer>". "F <peer>" also restarts the device's
    unconfirmed receipts of messages it sent to that peer; "F *" restarts no receipts.
    """
    return run_in_service_transaction(
        lambda: start_refresh_in_transaction(device.pk, request, now, requested_by_inbound_id)
    )


def start_refresh_in_transaction(
    device_id: int,
    request: RefreshRequest,
    now: datetime,
    requested_by_inbound_id: int | None,
) -> RefreshRequestOutcome:
    device_user_id = Contact.objects.filter(id=device_id).values_list("user_id", flat=True).first()
    if device_user_id is None:
        return build_refresh_error_outcome(ErrorCode.NOT_SIGNED_IN, request, "the device is not signed in")

    if request.is_for_all_peers:
        peer_ids = find_peer_ids_with_missed_messages(device_id, device_user_id)
    else:
        peer = User.objects.filter(username_lookup=normalize_username_for_lookup(request.refresh_target)).first()
        if peer is None:
            return build_refresh_error_outcome(ErrorCode.NO_SUCH_USER, request, "no such user")
        if peer.pk == device_user_id:
            return build_refresh_error_outcome(ErrorCode.SELF, request, "the peer is the user itself")
        peer_ids = [peer.pk]

    locked_users_by_id = lock_users_then_device(device_user_id, peer_ids, device_id)
    locked_peers = [locked_users_by_id[peer_id] for peer_id in sorted(peer_ids) if peer_id in locked_users_by_id]
    if not request.is_for_all_peers and not locked_peers:
        return build_refresh_error_outcome(ErrorCode.NO_SUCH_USER, request, "no such user")

    lock_deliveries_a_refresh_may_change(device_id, device_user_id, [peer.pk for peer in locked_peers])
    if not request.is_for_all_peers:
        rearm_receipts_for_refresh(device_id, device_user_id, locked_peers[0].pk, now)

    peer_refreshes = [
        refresh_peer(device_id, device_user_id, peer.pk, request.is_for_all_peers, requested_by_inbound_id, now)
        for peer in locked_peers
    ]
    total_message_count = sum(peer_refresh.message_count for peer_refresh in peer_refreshes)
    started_session_count = sum(peer_refresh.started_new_session for peer_refresh in peer_refreshes)

    reply_target = request.refresh_target if request.is_for_all_peers else locked_peers[0].username
    return RefreshRequestOutcome(
        reply=RefreshReply(refresh_target=reply_target, message_count=total_message_count),
        outcome_summary=(
            f"{total_message_count} message(s) to re-deliver, "
            f"{started_session_count} new refresh session(s) of {len(peer_refreshes)} peer(s)"
        ),
    )


def build_refresh_error_outcome(error_code: ErrorCode, request: RefreshRequest, reason: str) -> RefreshRequestOutcome:
    return RefreshRequestOutcome(reply=build_request_error_reply(error_code, request), outcome_summary=reason)


def find_peer_ids_with_missed_messages(device_id: int, user_id: int) -> list[int]:
    """For "F *": every sender of a message in the refresh scope, and every peer of an active session."""
    senders_of_taken_over_deliveries = select_deliveries_a_refresh_takes_over(device_id, user_id).values_list(
        "message__sender_id", flat=True
    )
    senders_of_missed_messages = select_messages_no_device_received(device_id, user_id).values_list(
        "sender_id", flat=True
    )
    peers_of_active_sessions = RefreshSession.objects.filter(
        device_id=device_id, state=RefreshSession.State.ACTIVE
    ).values_list("peer_id", flat=True)
    peer_ids = {*senders_of_taken_over_deliveries, *senders_of_missed_messages, *peers_of_active_sessions}
    peer_ids.discard(user_id)
    return sorted(peer_ids)


def lock_users_then_device(device_user_id: int, peer_ids: list[int], device_id: int) -> dict[int, User]:
    """Lock the user and the peers in ascending id, then the device, and check the device still belongs to the user."""
    locked_users_by_id = {
        user.pk: user
        for user in User.objects.select_for_update().filter(id__in=[device_user_id, *peer_ids]).order_by("id")
    }
    locked_device_user_id = (
        Contact.objects.select_for_update().filter(id=device_id).values_list("user_id", flat=True).first()
    )
    if device_user_id not in locked_users_by_id or locked_device_user_id != device_user_id:
        raise LockedRowChangedError(f"Device {device_id} changed its user while a refresh was being started.")
    return locked_users_by_id


def select_deliveries_a_refresh_takes_over(device_id: int, user_id: int) -> QuerySet[MessageDelivery]:
    """The device's failed, relink-cancelled and not yet owned pending deliveries of accepted messages to the user."""
    return MessageDelivery.objects.filter(
        Q(state=MessageDelivery.State.FAILED)
        | Q(state=MessageDelivery.State.CANCELLED)
        | Q(state=MessageDelivery.State.PENDING, refresh_session__isnull=True),
        device_id=device_id,
        message__recipient_id=user_id,
        message__accepted_at__isnull=False,
    )


def select_messages_no_device_received(device_id: int, user_id: int) -> QuerySet[Message]:
    """Accepted messages to the user that no device received and that have no delivery row for this device."""
    return Message.objects.filter(
        recipient_id=user_id,
        accepted_at__isnull=False,
        delivered_at__isnull=True,
    ).exclude(deliveries__device_id=device_id)


def lock_deliveries_a_refresh_may_change(device_id: int, user_id: int, peer_ids: list[int]) -> None:
    """Lock, in ascending id, the deliveries a refresh takes over and the heads it may restart."""
    heads_of_active_sessions = Q(
        refresh_session__state=RefreshSession.State.ACTIVE,
        refresh_session__device_id=device_id,
        refresh_session__peer_id__in=peer_ids,
        state=MessageDelivery.State.PENDING,
    )
    deliveries_taken_over = Q(
        id__in=select_deliveries_a_refresh_takes_over(device_id, user_id)
        .filter(message__sender_id__in=peer_ids)
        .values("id")
    )
    list(
        MessageDelivery.objects.select_for_update(of=("self",))
        .filter(heads_of_active_sessions | deliveries_taken_over)
        .order_by("id")
        .values_list("id", flat=True)
    )


def refresh_peer(
    device_id: int,
    user_id: int,
    peer_id: int,
    is_for_all_peers: bool,
    requested_by_inbound_id: int | None,
    now: datetime,
) -> PeerRefresh:
    active_session = RefreshSession.objects.filter(
        device_id=device_id, peer_id=peer_id, state=RefreshSession.State.ACTIVE
    ).first()
    if active_session is not None:
        restart_active_session_head(active_session.pk, now)
        return PeerRefresh(message_count=count_outstanding_deliveries(active_session.pk), started_new_session=False)

    deliveries_taken_over = list(
        select_deliveries_a_refresh_takes_over(device_id, user_id).filter(message__sender_id=peer_id).order_by("id")
    )
    missed_message_ids = list(
        select_messages_no_device_received(device_id, user_id)
        .filter(sender_id=peer_id)
        .order_by("id")
        .values_list("id", flat=True)
    )
    scope_size = len(deliveries_taken_over) + len(missed_message_ids)
    if scope_size == 0:
        return PeerRefresh(message_count=0, started_new_session=False)

    refresh_session = RefreshSession.objects.create(
        device_id=device_id,
        peer_id=peer_id,
        state=RefreshSession.State.ACTIVE,
        requested_at=now,
        requested_for_all_peers=is_for_all_peers,
        messages_total=scope_size,
        requested_by_inbound_id=requested_by_inbound_id,
    )
    for delivery in deliveries_taken_over:
        take_delivery_into_refresh_session(delivery, refresh_session.pk, now)
    for message_id in missed_message_ids:
        create_refresh_owned_delivery(message_id, device_id, refresh_session.pk, now)
    arm_next_refresh_session_head(refresh_session.pk, now)
    return PeerRefresh(message_count=scope_size, started_new_session=True)


def restart_active_session_head(refresh_session_id: int, now: datetime) -> None:
    head = (
        MessageDelivery.objects.select_for_update()
        .filter(refresh_session_id=refresh_session_id, state=MessageDelivery.State.PENDING)
        .first()
    )
    if head is not None:
        restart_refresh_session_head(head, now)


def count_outstanding_deliveries(refresh_session_id: int) -> int:
    return MessageDelivery.objects.filter(
        refresh_session_id=refresh_session_id,
        state__in=OUTSTANDING_DELIVERY_STATES,
    ).count()


def find_active_refresh_session_id(delivery: MessageDelivery) -> int | None:
    """The session whose head this delivery is, or None when it is not a head of an active session."""
    if delivery.refresh_session_id is None:
        return None
    is_active = RefreshSession.objects.filter(
        id=delivery.refresh_session_id,
        state=RefreshSession.State.ACTIVE,
    ).exists()
    return delivery.refresh_session_id if is_active else None


def arm_next_refresh_session_head(refresh_session_id: int, now: datetime) -> MessageDelivery | None:
    """The owned queued delivery whose message comes first in acceptance order becomes the head."""
    next_head = (
        MessageDelivery.objects.select_for_update(of=("self",))
        .filter(refresh_session_id=refresh_session_id, state=MessageDelivery.State.QUEUED_FOR_REFRESH)
        .order_by("message__accepted_at", "message_id")
        .first()
    )
    if next_head is None:
        return None
    arm_refresh_session_head(next_head, now)
    return next_head


def complete_refresh_session(refresh_session_id: int, now: datetime) -> bool:
    return (
        RefreshSession.objects.filter(id=refresh_session_id, state=RefreshSession.State.ACTIVE).update(
            state=RefreshSession.State.COMPLETED,
            finished_at=now,
        )
        == 1
    )


def stop_refresh_session(refresh_session_id: int, now: datetime) -> None:
    """The head was given up: the messages queued behind it fail unsent, in the transaction that failed the head.

    The queued deliveries are locked after the head, out of the global lock order; a deadlock
    with a deletion is resolved by the transaction retry.
    """
    queued_deliveries = list(
        MessageDelivery.objects.select_for_update()
        .filter(refresh_session_id=refresh_session_id, state=MessageDelivery.State.QUEUED_FOR_REFRESH)
        .order_by("id")
    )
    for queued_delivery in queued_deliveries:
        fail_delivery_of_stopped_refresh(queued_delivery, now)
    was_stopped = (
        RefreshSession.objects.filter(id=refresh_session_id, state=RefreshSession.State.ACTIVE).update(
            state=RefreshSession.State.STOPPED,
            finished_at=now,
        )
        == 1
    )
    if was_stopped:
        logger.warning(
            "Refresh session %s stopped: its current message could not be delivered, %s queued message(s) failed.",
            refresh_session_id,
            len(queued_deliveries),
        )


def cancel_device_refresh_sessions_for_relink(device_id: int, now: datetime) -> int:
    """The device's deliveries are already cancelled; its active sessions end with them."""
    locked_session_ids = list(
        RefreshSession.objects.select_for_update()
        .filter(device_id=device_id, state=RefreshSession.State.ACTIVE)
        .order_by("id")
        .values_list("id", flat=True)
    )
    return RefreshSession.objects.filter(id__in=locked_session_ids).update(
        state=RefreshSession.State.CANCELLED,
        finished_at=now,
    )
