"""The worker's subscriptions to a connected client: synchronous callbacks that only queue what arrived.

The library calls a synchronous callback inline, in dispatch order, while it spawns an
asynchronous one as a task of its own, whose order is lost once it awaits, and which can hang
a disconnect. So every callback here builds a small immutable object and puts it in a queue,
and none of them touches the database or the node. Every callback also tells the gateway that
the node is alive.

Not subscribed: RX_LOG_DATA, which arrives for every packet the radio hears, and channel
traffic, which the drain reads and drops.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from meshcore import EventType

from messaging.inbound_log import ReceivedDirectMessageFrame
from worker.clock import Clock
from worker.node_gateway import NodeGateway
from worker.worker_state import WorkerSignals

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class NodeAcknowledgement:
    # Lower-case hex of the four-byte code.
    code: str
    round_trip_milliseconds: int | None


@dataclass(frozen=True, kw_only=True)
class PathUpdated:
    public_key: str


@dataclass(frozen=True, kw_only=True)
class AdvertHeard:
    """An advert from a node the relay node does not store (NEW_CONTACT), signature already verified."""

    contact_record: Mapping[str, Any]


@dataclass(frozen=True, kw_only=True)
class KnownContactAdvertised:
    public_key: str


@dataclass(frozen=True, kw_only=True)
class NodeDeletedContact:
    public_key: str


@dataclass(frozen=True, kw_only=True)
class NodeContactTableFull:
    pass


type NodeEvent = (
    NodeAcknowledgement | PathUpdated | AdvertHeard | KnownContactAdvertised | NodeDeletedContact | NodeContactTableFull
)


class NodeEventSubscriptions:
    def __init__(
        self,
        *,
        gateway: NodeGateway,
        inbound_frame_queue: asyncio.Queue[ReceivedDirectMessageFrame],
        node_event_queue: asyncio.Queue[NodeEvent],
        signals: WorkerSignals,
        clock: Clock,
        report_disconnection: Callable[[str], None],
    ) -> None:
        self._gateway = gateway
        self._inbound_frame_queue = inbound_frame_queue
        self._node_event_queue = node_event_queue
        self._signals = signals
        self._clock = clock
        self._report_disconnection = report_disconnection
        self._subscriptions: list[Any] = []

    def subscribe(self, meshcore_client: Any) -> None:
        callbacks_by_event_type: dict[Any, Callable[[Any], None]] = {
            EventType.CONTACT_MSG_RECV: self._queue_direct_message,
            EventType.MESSAGES_WAITING: self._request_drain,
            EventType.ACK: self._queue_acknowledgement,
            EventType.PATH_UPDATE: self._queue_path_update,
            EventType.NEW_CONTACT: self._queue_heard_advert,
            EventType.ADVERTISEMENT: self._queue_known_contact_advert,
            EventType.CONTACT_DELETED: self._queue_deleted_contact,
            EventType.CONTACTS_FULL: self._queue_full_contact_table,
            EventType.DISCONNECTED: self._handle_disconnection,
        }
        self._subscriptions = [
            meshcore_client.subscribe(event_type, callback) for event_type, callback in callbacks_by_event_type.items()
        ]

    def unsubscribe(self) -> None:
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions = []

    def _queue_direct_message(self, event: Any) -> None:
        self._gateway.record_node_push()
        payload = event.payload
        self._inbound_frame_queue.put_nowait(
            ReceivedDirectMessageFrame(
                sender_public_key_prefix=str(payload["pubkey_prefix"]).lower(),
                sender_timestamp=int(payload["sender_timestamp"]),
                text=replace_nul_characters(str(payload.get("text", ""))),
                text_type=int(payload.get("txt_type", 0)),
                path_length=int(payload["path_len"]),
                signal_to_noise_ratio=payload.get("SNR"),
                received_at=self._clock.now(),
            )
        )

    def _request_drain(self, _event: Any) -> None:
        self._gateway.record_node_push()
        self._signals.drain_requested.set()

    def _queue_acknowledgement(self, event: Any) -> None:
        self._gateway.record_node_push()
        code = str(event.payload.get("code", "")).lower()
        if not code:
            return
        round_trip_milliseconds = event.payload.get("trip_time")
        self._node_event_queue.put_nowait(
            NodeAcknowledgement(
                code=code,
                round_trip_milliseconds=int(round_trip_milliseconds) if round_trip_milliseconds is not None else None,
            )
        )

    def _queue_path_update(self, event: Any) -> None:
        self._gateway.record_node_push()
        self._node_event_queue.put_nowait(PathUpdated(public_key=str(event.payload["public_key"]).lower()))

    def _queue_heard_advert(self, event: Any) -> None:
        self._gateway.record_node_push()
        self._node_event_queue.put_nowait(AdvertHeard(contact_record=dict(event.payload)))

    def _queue_known_contact_advert(self, event: Any) -> None:
        self._gateway.record_node_push()
        self._node_event_queue.put_nowait(KnownContactAdvertised(public_key=str(event.payload["public_key"]).lower()))

    def _queue_deleted_contact(self, event: Any) -> None:
        self._gateway.record_node_push()
        self._node_event_queue.put_nowait(NodeDeletedContact(public_key=str(event.payload["pubkey"]).lower()))

    def _queue_full_contact_table(self, _event: Any) -> None:
        self._gateway.record_node_push()
        self._node_event_queue.put_nowait(NodeContactTableFull())

    def _handle_disconnection(self, event: Any) -> None:
        self._report_disconnection(str(event.payload.get("reason", "unknown")))


def replace_nul_characters(text: str) -> str:
    """PostgreSQL cannot store NUL in text, and a frame that cannot be recorded would be lost with its sender's retries.

    No protocol message contains NUL, so the replaced text is classified exactly as the original would be.
    """
    return text.replace("\x00", "\N{REPLACEMENT CHARACTER}")
