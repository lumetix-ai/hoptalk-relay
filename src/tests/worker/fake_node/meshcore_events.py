from typing import Any

from meshcore import EventType

from tests.worker.fake_node.waiting import wait_until


class MeshCoreEventRecorder:
    """Records, in dispatch order, the events of a meshcore client through synchronous callbacks."""

    def __init__(self, meshcore_client: Any, *event_types: Any) -> None:
        self.events: list[Any] = []
        subscribed_types = event_types or (None,)
        self._subscriptions = [
            meshcore_client.subscribe(event_type, self.events.append) for event_type in subscribed_types
        ]

    def of_type(self, event_type: Any) -> list[Any]:
        return [event for event in self.events if event.type == event_type]

    def types(self) -> list[Any]:
        return [event.type for event in self.events]

    async def wait_for_event(self, event_type: Any, *, count: int = 1, timeout_seconds: float = 2.0) -> Any:
        """The `count`-th event of that type, once it has been dispatched."""
        await wait_until(
            lambda: len(self.of_type(event_type)) >= count,
            timeout_seconds=timeout_seconds,
            description=f"{count} {event_type} event(s)",
        )
        return self.of_type(event_type)[count - 1]

    def stop(self) -> None:
        for subscription in self._subscriptions:
            subscription.unsubscribe()


async def wait_until_earlier_node_frames_are_dispatched(meshcore_client: Any) -> None:
    """Every frame the node wrote before this call has been dispatched to the subscribers.

    A quiet mesh says nothing about pushes still on their way over the serial link. The node's
    bytes reach the host in order and the library dispatches events in the order it reads them,
    so the reply to a harmless command arrives after every earlier push.
    """
    await meshcore_client.commands.get_time()


def is_error_with_code(event: Any, error_code: int) -> bool:
    return bool(event.type == EventType.ERROR and event.payload.get("error_code") == error_code)


def is_lost_reply(event: Any) -> bool:
    """meshcore's result for a command whose reply never came: an ERROR it made up itself."""
    return bool(event.type == EventType.ERROR and event.payload.get("reason") == "no_event_received")
