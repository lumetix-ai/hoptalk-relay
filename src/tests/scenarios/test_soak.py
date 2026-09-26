"""A randomised soak: lossy links, devices that come and go, worker crashes and a node reboot, and nothing is lost.

Every random choice (who sends what to whom, the part counts, the troubles and their times) comes
from SOAK_SEED; the mesh draws its losses, duplicates and reorderings from the fake node's seed.

Two hundred messages keep the relay's pacing busy for about an hour of production time, so the soak
runs twice as fast as the other scenarios: every duration a client can observe is the production
one multiplied by SOAK_TIME_FACTOR, on the clients and on the server alike. The firmware-ACK floors
are the exception: at this speed they would end before the fake mesh's round trip, so they stay
near the other scenarios' values. Most messages are short, as in a chat; long ones up to ten parts
are rarer.

The relay's own work is not scaled: every direct message costs it a few database transactions in
real time. When a slow machine makes the relay answer later than a client's scaled retry pause,
the client sends every unconfirmed part again; the copies fill the relay node's 256-frame queue,
the relay works through them one by one, and acceptance slows to a crawl while the planned
messages keep coming. That is why the soak is not faster: a smaller SOAK_TIME_FACTOR shortens the
retry pauses further. It is also why the troubled stretch runs on a scenario clock that stands
still while more than MAXIMUM_PARTS_IN_FLIGHT parts wait to be confirmed: it stops only while the
relay is behind, so a slower machine gets the same messages and troubles in the same order, only
later. And a convergence wait fails only once what is left has not shrunk for
NO_PROGRESS_TIMEOUT_SECONDS: then the relay is stuck, not slow.
"""

import asyncio
import random
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace

import pytest
from django.db.models import Sum
from pytest_django import Settings

from hoptalk_relay.relay_settings import RelaySettings
from messaging.models import InboundDirectMessage, Message, MessageDelivery, OutboundPacket, ReceiptNotification
from protocol.constants import MAXIMUM_PART_COUNT, PART_TEXT_MAXIMUM_BYTES
from tests.invariants import assert_all_invariants
from tests.scenarios.refresh_lifecycle_admin_helpers import (
    kill_relay_worker,
    start_new_relay_worker_process,
    start_relay_with_devices_from_cards,
)
from tests.scenarios.scenario_settings import (
    SCENARIO_ENGINE_TIMING,
    SCENARIO_PACING,
    SCENARIO_RETRY_STRATEGY,
    SCENARIO_TIME_FACTOR,
    SCENARIO_WORKER_TIMING,
)
from tests.scenarios.scenario_setup import sign_in
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.simulated_mesh import LinkPolicy, SimulatedDevice, SimulatedMesh
from tests.worker.fake_node.waiting import wait_until
from tests.worker.relay_worker.worker_harness import RelayWorkerHarness, in_database
from tests.worker.simulated_hoptalk_client import SimulatedHopTalkClient
from tests.worker.simulated_hoptalk_client_records import OutgoingMessage, OutgoingMessageStatus
from tests.worker.simulated_hoptalk_client_timing import ClientTiming, ScaledClock

pytestmark = pytest.mark.django_db(transaction=True)

SOAK_SEED = 34
SOAK_TIME_FACTOR = 0.005
SPEED_UP_OVER_THE_SCENARIOS = SOAK_TIME_FACTOR / SCENARIO_TIME_FACTOR
SOAK_MINIMUM_ACKNOWLEDGEMENT_WAIT_SECONDS = 0.03
SOAK_UNKNOWN_ACKNOWLEDGEMENT_WAIT_SECONDS = 0.06
SOAK_CLIENT_TIMING = ClientTiming().scaled_by(SOAK_TIME_FACTOR)
SOAK_WORKER_TIMING = replace(
    SCENARIO_WORKER_TIMING,
    incomplete_status_coalescing_seconds=(
        SCENARIO_WORKER_TIMING.incomplete_status_coalescing_seconds * SPEED_UP_OVER_THE_SCENARIOS
    ),
    identical_reply_suppression_seconds=(
        SCENARIO_WORKER_TIMING.identical_reply_suppression_seconds * SPEED_UP_OVER_THE_SCENARIOS
    ),
)

DEVICE_USERNAMES = {
    "alice-phone": "alice",
    "alice-tablet": "alice",
    "bob-phone": "bob",
    "bob-tablet": "bob",
    "carol-phone": "carol",
    "carol-tablet": "carol",
    "dave-phone": "dave",
    "erin-phone": "erin",
}
MESSAGE_COUNT = 200
# Relative frequencies of 1, 2, ... 10 parts: most messages are short, every length occurs.
PART_COUNT_WEIGHTS = (45, 20, 10, 7, 5, 4, 3, 2, 2, 2)
LOSS_PROBABILITY = 0.2
DUPLICATE_PROBABILITY = 0.05
REORDER_PROBABILITY = 0.1
REORDER_DELAY_SECONDS = 10 * SOAK_TIME_FACTOR
# The messages are sent and the troubles below happen within this stretch; afterwards the mesh heals.
TROUBLED_SECONDS = 6000 * SOAK_TIME_FACTOR
DEVICES_SWITCHED_OFF = 4
PHONES_AWAY = 3
ASYMMETRIC_LINKS = 2
# How long a device is off, its phone away or its link asymmetric, as a share of the troubled stretch.
TROUBLE_SHARE_RANGE = (0.05, 0.25)
WORKER_CRASH_SHARES = (0.3, 0.7)
NODE_REBOOT_SHARE = 0.5
WORKER_RESTART_DELAY_SECONDS = 100 * SOAK_TIME_FACTOR
# More than the longest message, so that one long message in flight does not hold the next one back.
MAXIMUM_PARTS_IN_FLIGHT = 12
NO_PROGRESS_TIMEOUT_SECONDS = 60.0
UPLOAD_POLL_INTERVAL_SECONDS = 0.01
# The worker's database work and these checks share one thread, so the checks leave it room.
DATABASE_POLL_INTERVAL_SECONDS = 0.1
SPARED_ROUTE_RESET_STATES = (
    OutboundPacket.RouteResetState.SKIPPED_LATE_ACKNOWLEDGEMENT,
    OutboundPacket.RouteResetState.SKIPPED_PATH_UPDATE,
    OutboundPacket.RouteResetState.SKIPPED_APPLICATION_EVIDENCE,
)


@pytest.fixture(autouse=True)
def run_the_relay_at_the_soak_speed(use_scenario_relay_settings: None, settings: Settings) -> None:
    relay_settings: RelaySettings = settings.RELAY_SETTINGS
    settings.RELAY_SETTINGS = replace(
        relay_settings,
        retry_strategy=replace(
            SCENARIO_RETRY_STRATEGY,
            initial_pause_seconds=SCENARIO_RETRY_STRATEGY.initial_pause_seconds * SPEED_UP_OVER_THE_SCENARIOS,
            maximum_pause_seconds=SCENARIO_RETRY_STRATEGY.maximum_pause_seconds * SPEED_UP_OVER_THE_SCENARIOS,
            delivered_receipt_delay_seconds=(
                SCENARIO_RETRY_STRATEGY.delivered_receipt_delay_seconds * SPEED_UP_OVER_THE_SCENARIOS
            ),
        ),
        pacing=replace(
            SCENARIO_PACING,
            minimum_seconds_between_sends=SCENARIO_PACING.minimum_seconds_between_sends * SPEED_UP_OVER_THE_SCENARIOS,
        ),
        engine_timing=replace(
            SCENARIO_ENGINE_TIMING,
            minimum_acknowledgement_wait_seconds=SOAK_MINIMUM_ACKNOWLEDGEMENT_WAIT_SECONDS,
            unknown_acknowledgement_wait_seconds=SOAK_UNKNOWN_ACKNOWLEDGEMENT_WAIT_SECONDS,
            missing_parts_round_delay_seconds=(
                SCENARIO_ENGINE_TIMING.missing_parts_round_delay_seconds * SPEED_UP_OVER_THE_SCENARIOS
            ),
            recent_path_update_seconds=SCENARIO_ENGINE_TIMING.recent_path_update_seconds * SPEED_UP_OVER_THE_SCENARIOS,
            flood_arrival_reset_maximum_age_seconds=(
                SCENARIO_ENGINE_TIMING.flood_arrival_reset_maximum_age_seconds * SPEED_UP_OVER_THE_SCENARIOS
            ),
            reply_resend_maximum_age_seconds=(
                SCENARIO_ENGINE_TIMING.reply_resend_maximum_age_seconds * SPEED_UP_OVER_THE_SCENARIOS
            ),
        ),
    )


@pytest.fixture
async def soak_clients() -> AsyncIterator[dict[str, SimulatedHopTalkClient]]:
    """The clients the soak starts at its own speed; each is stopped afterwards and must not have raised."""
    started_clients: dict[str, SimulatedHopTalkClient] = {}
    yield started_clients
    for client in started_clients.values():
        await client.stop()
    client_errors = [
        f"{client!r}: {client.internal_errors!r}" for client in started_clients.values() if client.internal_errors
    ]
    assert client_errors == [], "a simulated client raised internally"


class ScenarioClock:
    """Seconds since the troubled stretch began, without the time spent waiting for the relay to catch up."""

    def __init__(self) -> None:
        self._started_at = time.monotonic()
        self._paused_seconds = 0.0

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started_at - self._paused_seconds

    async def sleep_until(self, scenario_seconds: float) -> None:
        await asyncio.sleep(max(0.0, scenario_seconds - self.elapsed_seconds()))

    def add_pause(self, paused_seconds: float) -> None:
        self._paused_seconds += paused_seconds


@dataclass(frozen=True, kw_only=True)
class TimedAction:
    at_seconds: float
    description: str
    perform: Callable[[], Awaitable[None]]


@dataclass(frozen=True, kw_only=True)
class SoakMessage:
    sender_device_name: str
    recipient_username: str
    text: str


def build_lossy_link(*, acknowledgement_loss_probability: float | None = None) -> LinkPolicy:
    return LinkPolicy(
        loss_probability=LOSS_PROBABILITY,
        acknowledgement_loss_probability=acknowledgement_loss_probability,
        duplicate_probability=DUPLICATE_PROBABILITY,
        duplicates_bypass_deduplication=True,
        reorder_probability=REORDER_PROBABILITY,
        reorder_delay_seconds=REORDER_DELAY_SECONDS,
    )


def build_message_text(random_generator: random.Random, message_number: int, sender_username: str) -> str:
    [part_count] = random_generator.choices(range(1, MAXIMUM_PART_COUNT + 1), weights=PART_COUNT_WEIGHTS)
    header = f"#{message_number} from {sender_username}, {part_count} parts: "
    minimum_length = (part_count - 1) * PART_TEXT_MAXIMUM_BYTES + 1
    maximum_length = part_count * PART_TEXT_MAXIMUM_BYTES
    text_length = max(len(header) + 1, random_generator.randint(minimum_length, maximum_length))
    filler = "".join(random_generator.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(text_length))
    return (header + filler)[:text_length]


def plan_messages(random_generator: random.Random) -> list[tuple[float, SoakMessage]]:
    usernames = sorted(set(DEVICE_USERNAMES.values()))
    planned_messages: list[tuple[float, SoakMessage]] = []
    for message_number in range(1, MESSAGE_COUNT + 1):
        sender_device_name = random_generator.choice(sorted(DEVICE_USERNAMES))
        sender_username = DEVICE_USERNAMES[sender_device_name]
        recipient_username = random_generator.choice([name for name in usernames if name != sender_username])
        soak_message = SoakMessage(
            sender_device_name=sender_device_name,
            recipient_username=recipient_username,
            text=build_message_text(random_generator, message_number, sender_username),
        )
        planned_messages.append((random_generator.uniform(0, TROUBLED_SECONDS), soak_message))
    return sorted(planned_messages, key=lambda planned_message: planned_message[0])


def draw_trouble_period(random_generator: random.Random) -> tuple[float, float]:
    duration_seconds = random_generator.uniform(*TROUBLE_SHARE_RANGE) * TROUBLED_SECONDS
    start_seconds = random_generator.uniform(0, TROUBLED_SECONDS - duration_seconds)
    return start_seconds, start_seconds + duration_seconds


def run_now(action: Callable[[], None]) -> Callable[[], Awaitable[None]]:
    async def perform() -> None:
        action()

    return perform


def plan_trouble_period(
    random_generator: random.Random,
    description: str,
    begin_trouble: Callable[[], None],
    end_trouble: Callable[[], None],
) -> list[TimedAction]:
    start_seconds, end_seconds = draw_trouble_period(random_generator)
    return [
        TimedAction(at_seconds=start_seconds, description=f"{description} begins", perform=run_now(begin_trouble)),
        TimedAction(at_seconds=end_seconds, description=f"{description} ends", perform=run_now(end_trouble)),
    ]


def plan_device_troubles(random_generator: random.Random, devices: dict[str, SimulatedDevice]) -> list[TimedAction]:
    device_names = sorted(devices)
    timed_actions: list[TimedAction] = []
    for device_name in random_generator.sample(device_names, DEVICES_SWITCHED_OFF):
        device = devices[device_name]
        timed_actions += plan_trouble_period(
            random_generator, f"{device_name} switched off", device.switch_off, device.switch_on
        )
    for device_name in random_generator.sample(device_names, PHONES_AWAY):
        device = devices[device_name]
        timed_actions += plan_trouble_period(
            random_generator, f"{device_name}'s phone away", device.phone_leaves, device.phone_returns
        )
    for device_name in random_generator.sample(device_names, ASYMMETRIC_LINKS):
        device = devices[device_name]
        losing_acknowledgements_on_uplink = random_generator.random() < 0.5

        def lose_every_firmware_acknowledgement_one_way(
            device: SimulatedDevice = device, on_uplink: bool = losing_acknowledgements_on_uplink
        ) -> None:
            if on_uplink:
                device.uplink = build_lossy_link(acknowledgement_loss_probability=1.0)
            else:
                device.downlink = build_lossy_link(acknowledgement_loss_probability=1.0)

        def make_the_links_symmetric_again(device: SimulatedDevice = device) -> None:
            device.uplink = build_lossy_link()
            device.downlink = build_lossy_link()

        timed_actions += plan_trouble_period(
            random_generator,
            f"{device_name}'s asymmetric link",
            lose_every_firmware_acknowledgement_one_way,
            make_the_links_symmetric_again,
        )
    return timed_actions


def plan_relay_troubles(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> list[TimedAction]:
    async def crash_the_worker() -> None:
        await kill_relay_worker(relay_worker)
        await asyncio.sleep(WORKER_RESTART_DELAY_SECONDS)
        start_new_relay_worker_process(relay_worker)

    crashes = [
        TimedAction(at_seconds=share * TROUBLED_SECONDS, description="worker crash", perform=crash_the_worker)
        for share in WORKER_CRASH_SHARES
    ]
    reboot = TimedAction(
        at_seconds=NODE_REBOOT_SHARE * TROUBLED_SECONDS,
        description="node reboot",
        perform=run_now(fake_companion_firmware.reboot),
    )
    return [*crashes, reboot]


async def wait_until_no_work_remains(
    read_remaining_work: Callable[[], Awaitable[int]], *, poll_interval_seconds: float, description: str
) -> None:
    """Wait until the remaining work is zero; fail once it has not reached a new low for NO_PROGRESS_TIMEOUT_SECONDS."""
    smallest_remaining_work = await read_remaining_work()
    last_progress_at = time.monotonic()
    while smallest_remaining_work > 0:
        await asyncio.sleep(poll_interval_seconds)
        remaining_work = await read_remaining_work()
        if remaining_work < smallest_remaining_work:
            smallest_remaining_work = remaining_work
            last_progress_at = time.monotonic()
        elif time.monotonic() - last_progress_at >= NO_PROGRESS_TIMEOUT_SECONDS:
            raise AssertionError(
                f"No progress for {NO_PROGRESS_TIMEOUT_SECONDS} s towards {description}: {remaining_work} left"
            )


def count_parts_in_flight(
    clients: dict[str, SimulatedHopTalkClient], sent_messages: list[tuple[str, OutgoingMessage]]
) -> int:
    """Parts the relay has not confirmed yet of messages whose client can reach its node and so keeps sending them.

    A sender that cannot reach its node comes back only when its trouble ends, which waits for the
    scenario clock: counting its parts would stop the clock for good.
    """
    return sum(
        len(outgoing_message.missing_part_numbers())
        for sender_device_name, outgoing_message in sent_messages
        if outgoing_message.status is OutgoingMessageStatus.PENDING
        and clients[sender_device_name].device.app_can_reach_node
    )


async def wait_for_the_relay_to_catch_up(
    clients: dict[str, SimulatedHopTalkClient], sent_messages: list[tuple[str, OutgoingMessage]]
) -> float:
    """Wait until at most MAXIMUM_PARTS_IN_FLIGHT parts are in flight; returns the seconds waited."""
    waiting_started_at = time.monotonic()

    async def count_parts_over_the_limit() -> int:
        return max(0, count_parts_in_flight(clients, sent_messages) - MAXIMUM_PARTS_IN_FLIGHT)

    await wait_until_no_work_remains(
        count_parts_over_the_limit,
        poll_interval_seconds=UPLOAD_POLL_INTERVAL_SECONDS,
        description=f"at most {MAXIMUM_PARTS_IN_FLIGHT} parts waiting to be confirmed",
    )
    return time.monotonic() - waiting_started_at


async def run_the_troubled_stretch(
    clients: dict[str, SimulatedHopTalkClient],
    planned_messages: list[tuple[float, SoakMessage]],
    timed_actions: list[TimedAction],
) -> list[tuple[str, OutgoingMessage]]:
    """Send every planned message and perform every trouble at its scenario time; returns (sender device, message)."""
    scenario_clock = ScenarioClock()
    sent_messages: list[tuple[str, OutgoingMessage]] = []
    schedule: list[tuple[float, SoakMessage | TimedAction]] = [
        *planned_messages,
        *((timed_action.at_seconds, timed_action) for timed_action in timed_actions),
    ]
    for at_seconds, scheduled_item in sorted(schedule, key=lambda item: item[0]):
        await scenario_clock.sleep_until(at_seconds)
        scenario_clock.add_pause(await wait_for_the_relay_to_catch_up(clients, sent_messages))
        if isinstance(scheduled_item, TimedAction):
            await scheduled_item.perform()
            continue
        sender_client = clients[scheduled_item.sender_device_name]
        outgoing_message = sender_client.send_message(scheduled_item.recipient_username, scheduled_item.text)
        sent_messages.append((scheduled_item.sender_device_name, outgoing_message))
    return sent_messages


def heal_the_mesh(devices: dict[str, SimulatedDevice]) -> None:
    for device in devices.values():
        device.uplink = LinkPolicy()
        device.downlink = LinkPolicy()
        if not device.is_switched_on:
            device.switch_on()
        if not device.phone_is_connected:
            device.phone_returns()


async def start_soak_client(
    soak_clients: dict[str, SimulatedHopTalkClient], device: SimulatedDevice, username: str, clock: ScaledClock
) -> SimulatedHopTalkClient:
    client = SimulatedHopTalkClient(device, timing=SOAK_CLIENT_TIMING, clock=clock)
    client.start()
    soak_clients[device.name] = client
    await sign_in(client, username)
    return client


def count_deliveries_in_progress() -> int:
    return MessageDelivery.objects.filter(
        state__in=[MessageDelivery.State.PENDING, MessageDelivery.State.QUEUED_FOR_REFRESH]
    ).count()


def count_deliveries_not_delivered() -> int:
    return MessageDelivery.objects.exclude(state=MessageDelivery.State.DELIVERED).count()


def count_deliveries_not_delivered_and_pending_receipts() -> int:
    return count_deliveries_not_delivered() + count_pending_receipts()


def read_deliveries_not_delivered() -> list[MessageDelivery]:
    return list(MessageDelivery.objects.exclude(state=MessageDelivery.State.DELIVERED).order_by("id"))


def count_pending_receipts() -> int:
    return ReceiptNotification.objects.filter(state=ReceiptNotification.State.PENDING).count()


async def count_pending_messages(sent_messages: list[tuple[str, OutgoingMessage]]) -> int:
    return sum(1 for _, outgoing_message in sent_messages if outgoing_message.status is OutgoingMessageStatus.PENDING)


def read_stored_texts_by_sender_and_id() -> dict[tuple[str, int], str | None]:
    return {
        (message.sender.username, message.client_message_id): message.text
        for message in Message.objects.select_related("sender")
    }


def read_receipts() -> list[ReceiptNotification]:
    return list(ReceiptNotification.objects.select_related("message"))


def count_firmware_repeats() -> int:
    firmware_repeat_count: int = InboundDirectMessage.objects.aggregate(total=Sum("duplicate_count"))["total"] or 0
    return firmware_repeat_count


def count_packets_by_route_reset_state() -> Counter[str]:
    return Counter(OutboundPacket.objects.values_list("route_reset_state", flat=True))


async def test_a_randomised_soak_ends_with_every_message_delivered_to_every_device_of_its_recipient(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    soak_clients: dict[str, SimulatedHopTalkClient],
) -> None:
    random_generator = random.Random(SOAK_SEED)
    relay_worker.timing = SOAK_WORKER_TIMING
    relay_worker.worker = relay_worker.build_worker()
    devices = await start_relay_with_devices_from_cards(
        relay_worker, fake_companion_firmware, simulated_mesh, DEVICE_USERNAMES
    )
    client_clock = ScaledClock(SOAK_TIME_FACTOR)
    clients = {
        device_name: await start_soak_client(soak_clients, devices[device_name], username, client_clock)
        for device_name, username in DEVICE_USERNAMES.items()
    }
    for device in devices.values():
        device.uplink = build_lossy_link()
        device.downlink = build_lossy_link()
    planned_messages = plan_messages(random_generator)
    timed_actions = [
        *plan_device_troubles(random_generator, devices),
        *plan_relay_troubles(relay_worker, fake_companion_firmware),
    ]

    sent_messages = await run_the_troubled_stretch(clients, planned_messages, timed_actions)
    heal_the_mesh(devices)
    await wait_until_no_work_remains(
        lambda: count_pending_messages(sent_messages),
        poll_interval_seconds=UPLOAD_POLL_INTERVAL_SECONDS,
        description="the relay to accept every message",
    )
    await wait_until_no_work_remains(
        lambda: in_database(count_deliveries_in_progress),
        poll_interval_seconds=DATABASE_POLL_INTERVAL_SECONDS,
        description="every delivery to be delivered or given up",
    )
    deliveries_given_up = await in_database(read_deliveries_not_delivered)
    for device_name, client in clients.items():
        for peer_username in sorted(set(DEVICE_USERNAMES.values()) - {DEVICE_USERNAMES[device_name]}):
            client.open_conversation(peer_username)
    await wait_until_no_work_remains(
        lambda: in_database(count_deliveries_not_delivered_and_pending_receipts),
        poll_interval_seconds=DATABASE_POLL_INTERVAL_SECONDS,
        description="every delivery to be delivered and every receipt settled",
    )
    await wait_until(
        lambda: all(
            outgoing_message.status is OutgoingMessageStatus.DELIVERED for _, outgoing_message in sent_messages
        ),
        description="every sender to see its messages delivered",
    )

    assert {outgoing_message.part_count for _, outgoing_message in sent_messages} == set(
        range(1, MAXIMUM_PART_COUNT + 1)
    )
    for delivery in deliveries_given_up:
        assert delivery.state == MessageDelivery.State.FAILED, delivery
        assert delivery.failure_reason == MessageDelivery.FailureReason.ATTEMPTS_EXHAUSTED, delivery
        assert delivery.attempt_count == delivery.maximum_attempts, delivery
    stored_texts = await in_database(read_stored_texts_by_sender_and_id)
    assert len(stored_texts) == len(sent_messages) == MESSAGE_COUNT
    for sender_device_name, outgoing_message in sent_messages:
        stored_key = (DEVICE_USERNAMES[sender_device_name], outgoing_message.message_id)
        assert stored_texts[stored_key] == outgoing_message.text
    for device_name, client in clients.items():
        username = DEVICE_USERNAMES[device_name]
        expected_texts = Counter(
            outgoing_message.text
            for _sender_device_name, outgoing_message in sent_messages
            if outgoing_message.recipient_username == username
        )
        displayed_texts = Counter(incoming_message.text for incoming_message in client.displayed_messages)
        assert displayed_texts == expected_texts, device_name
    for receipt in await in_database(read_receipts):
        if receipt.state != ReceiptNotification.State.CONFIRMED:
            assert receipt.state == ReceiptNotification.State.FAILED, receipt
            assert receipt.attempt_count == receipt.maximum_attempts, receipt
            assert receipt.device_id != receipt.message.sender_device_id, receipt
    assert await in_database(count_firmware_repeats) > 0
    route_reset_states = await in_database(count_packets_by_route_reset_state)
    assert route_reset_states[OutboundPacket.RouteResetState.PERFORMED] > 0
    # Which reason spares a route depends on what reached the relay first, so it varies with the
    # machine's load; the route scenarios pin each reason on its own.
    assert sum(route_reset_states[spared_state] for spared_state in SPARED_ROUTE_RESET_STATES) > 0
    await in_database(assert_all_invariants)
