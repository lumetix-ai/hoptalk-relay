"""The reference HopTalk client's state machines, against a small scripted server on the relay's fake node.

`ScriptedRelay` stands where the relay worker will stand: it reads the direct messages that reach
the relay's node and answers them through `answer`, which starts as a minimal well-behaved server
(sign-in, user checks, message parts with coalesced statuses, reads and refreshes) and which a
test replaces or wraps to lose, reorder or invent answers. Every duration is the protocol's,
scaled by `TEST_TIME_FACTOR`.
"""

import asyncio
import contextlib
import dataclasses
import itertools
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable

import pytest

from protocol.constants import (
    PROTOCOL_PREFIX,
    RECEIVED_SET_MISSING,
    RECEIVED_SET_RECEIVED,
    ClientMessageType,
    ErrorCode,
    ReceiptLevel,
)
from protocol.formatting import (
    format_account_reply,
    format_delivery_part,
    format_error_reply,
    format_message_part_request,
    format_query_reply,
    format_read_reply,
    format_receipt_push,
    format_refresh_reply,
    format_send_status_reply,
)
from protocol.message_types import (
    AccountRequest,
    MessagePartRequest,
    QueryRequest,
    ReadRequest,
    RefreshRequest,
)
from protocol.parsing import parse_direct_message_text
from protocol.usernames import normalize_username_for_lookup
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware, TextMessageQueued
from tests.worker.fake_node.frames import TextType
from tests.worker.fake_node.simulated_mesh import (
    LinkPolicy,
    ReceivedDirectMessage,
    SimulatedDevice,
    SimulatedMesh,
    parse_received_direct_message,
)
from tests.worker.simulated_hoptalk_client import ClientNotSignedInError, SimulatedHopTalkClient
from tests.worker.simulated_hoptalk_client_node_link import (
    RouteHygieneOutcome,
    RouteHygieneTrigger,
    SentDirectMessage,
)
from tests.worker.simulated_hoptalk_client_records import (
    FAILURE_REASON_ACCOUNT_SWITCHED,
    ClientEventKind,
    ConversationRefresh,
    OutgoingMessageStatus,
    ReceivedDirectMessageHandling,
    RequestState,
    SimulatedClientStorage,
)
from tests.worker.simulated_hoptalk_client_timing import ClientTiming, ScaledClock

TEST_TIME_FACTOR = 0.01
FAST_TIMING = ClientTiming().scaled_by(TEST_TIME_FACTOR)
PASSWORD = "correct horse battery"
OTHER_PASSWORD = "another horse battery"
RELAY_POLL_INTERVAL_SECONDS = 0.002
INCOMING_MESSAGE_ID = 1790294400123456
MESSAGE_TYPE_LETTER_POSITION = len(PROTOCOL_PREFIX)
ZERO_WIDTH_JOINER_FAMILY = "\U0001f468‍\U0001f469‍\U0001f467‍\U0001f466"

type MessageKey = tuple[str, int]


def message_type_letter_of(text: str) -> str:
    return text[MESSAGE_TYPE_LETTER_POSITION : MESSAGE_TYPE_LETTER_POSITION + 1]


class ScriptedRelay:
    """Plays the server on the relay's node for one device; see the module docstring."""

    def __init__(self, relay_firmware: FakeCompanionFirmware, device: SimulatedDevice) -> None:
        self.relay_firmware = relay_firmware
        self.device = device
        self.received_direct_messages: list[ReceivedDirectMessage] = []
        self.sent_texts: list[str] = []
        self.answer: Callable[[str], list[str]] = self.answer_like_a_server
        self.coalescing_seconds = FAST_TIMING.incomplete_acknowledgement_coalescing_seconds
        self._users: dict[str, tuple[str, str | None]] = {}
        self._unanswered_counts: Counter[str] = Counter()
        self._held_parts: dict[MessageKey, dict[int, str]] = {}
        self._part_counts: dict[MessageKey, int] = {}
        self._recipient_spellings: dict[MessageKey, str] = {}
        self._status_due_times: dict[MessageKey, float] = {}
        self._last_sender_timestamp = 0
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await self._task

    def add_user(self, username: str, password: str | None = None) -> None:
        """A user the server knows; without a password any sign-in as that user fails."""
        self._users[normalize_username_for_lookup(username)] = (username, password)

    def leave_unanswered(self, message_type_letter: str, count: int = 1) -> None:
        """The next `count` requests of that type get no answer, as if every answer to them was lost."""
        self._unanswered_counts[message_type_letter] += count

    def received_texts(self, message_type_letter: str | None = None) -> list[str]:
        return [
            received_direct_message.text
            for received_direct_message in self.received_direct_messages
            if message_type_letter is None
            or message_type_letter_of(received_direct_message.text) == message_type_letter
        ]

    def send(self, text: str, *, by_flood: bool = False) -> None:
        if by_flood:
            self.relay_firmware.reset_route(self.device.public_key)
        self._last_sender_timestamp = max(int(time.time()), self._last_sender_timestamp + 1)
        send_result = self.relay_firmware.send_text_message(
            text_type=TextType.PLAIN,
            attempt=0,
            sender_timestamp=self._last_sender_timestamp,
            recipient_public_key_prefix=self.device.public_key_prefix,
            text=text.encode(),
        )
        assert isinstance(send_result, TextMessageQueued), send_result
        self.sent_texts.append(text)

    async def _run(self) -> None:
        while True:
            self._serve_once()
            await asyncio.sleep(RELAY_POLL_INTERVAL_SECONDS)

    def _serve_once(self) -> None:
        while (queued_frame := self.relay_firmware.pop_offline_frame()) is not None:
            received_direct_message = parse_received_direct_message(queued_frame)
            if received_direct_message is not None:
                self._receive(received_direct_message)
        self._send_due_statuses()

    def _receive(self, received_direct_message: ReceivedDirectMessage) -> None:
        self.received_direct_messages.append(received_direct_message)
        if received_direct_message.arrived_by_flood:
            self.relay_firmware.reset_route(self.device.public_key)
        message_type_letter = message_type_letter_of(received_direct_message.text)
        if self._unanswered_counts[message_type_letter] > 0:
            self._unanswered_counts[message_type_letter] -= 1
            return
        for reply_text in self.answer(received_direct_message.text):
            self.send(reply_text)

    def answer_like_a_server(self, text: str) -> list[str]:
        match parse_direct_message_text(text):
            case AccountRequest() as account_request:
                return [self._answer_account_request(account_request)]
            case QueryRequest() as query_request:
                return [self._answer_query_request(query_request)]
            case MessagePartRequest() as message_part_request:
                return self._answer_message_part_request(message_part_request)
            case ReadRequest() as read_request:
                return [format_read_reply(read_request.sender_username, read_request.message_id)]
            case RefreshRequest() as refresh_request:
                return [format_refresh_reply(refresh_request.refresh_target, 0)]
        return []

    def _answer_account_request(self, account_request: AccountRequest) -> str:
        user = self._users.get(normalize_username_for_lookup(account_request.username))
        if user is None:
            self.add_user(account_request.username, account_request.password)
            return format_account_reply(account_request.username)
        canonical_username, password = user
        if password != account_request.password:
            return format_error_reply(
                ErrorCode.WRONG_PASSWORD, ClientMessageType.ACCOUNT_REQUEST, account_request.username
            )
        return format_account_reply(canonical_username)

    def _answer_query_request(self, query_request: QueryRequest) -> str:
        user = self._users.get(normalize_username_for_lookup(query_request.username))
        if user is None:
            return format_query_reply(query_request.username, False)
        return format_query_reply(user[0], True)

    def _answer_message_part_request(self, part_request: MessagePartRequest) -> list[str]:
        recipient = self._users.get(normalize_username_for_lookup(part_request.recipient_username))
        if recipient is None:
            reference = f"{part_request.recipient_username} {part_request.message_id}"
            return [format_error_reply(ErrorCode.NO_SUCH_USER, ClientMessageType.MESSAGE_PART_REQUEST, reference)]
        message_key = (normalize_username_for_lookup(part_request.recipient_username), part_request.message_id)
        self._held_parts.setdefault(message_key, {}).setdefault(part_request.part_number, part_request.part_text)
        self._part_counts[message_key] = part_request.part_count
        self._recipient_spellings[message_key] = recipient[0]
        if RECEIVED_SET_MISSING not in self._received_set(message_key):
            self._status_due_times.pop(message_key, None)
            return [self._format_status(message_key)]
        self._status_due_times[message_key] = time.monotonic() + self.coalescing_seconds
        return []

    def _received_set(self, message_key: MessageKey) -> str:
        held_parts = self._held_parts[message_key]
        part_count = self._part_counts[message_key]
        return "".join(
            RECEIVED_SET_RECEIVED if part_number in held_parts else RECEIVED_SET_MISSING
            for part_number in range(1, part_count + 1)
        )

    def _format_status(self, message_key: MessageKey) -> str:
        return format_send_status_reply(
            self._recipient_spellings[message_key], message_key[1], self._received_set(message_key)
        )

    def _send_due_statuses(self) -> None:
        now = time.monotonic()
        for message_key, due_time in list(self._status_due_times.items()):
            if now >= due_time:
                del self._status_due_times[message_key]
                self.send(self._format_status(message_key))


@pytest.fixture
async def device(simulated_mesh: SimulatedMesh) -> SimulatedDevice:
    """Async, because the device's node starts its main loop on the running event loop."""
    return simulated_mesh.add_device("tracker")


@pytest.fixture
async def relay(
    fake_companion_firmware: FakeCompanionFirmware, device: SimulatedDevice
) -> AsyncIterator[ScriptedRelay]:
    scripted_relay = ScriptedRelay(fake_companion_firmware, device)
    scripted_relay.add_user("Bob", PASSWORD)
    scripted_relay.add_user("ivan", PASSWORD)
    scripted_relay.start()
    yield scripted_relay
    await scripted_relay.stop()


@contextlib.asynccontextmanager
async def running_client(
    device: SimulatedDevice,
    *,
    timing: ClientTiming = FAST_TIMING,
    storage: SimulatedClientStorage | None = None,
    reads_messages_in_open_conversations: bool = False,
) -> AsyncIterator[SimulatedHopTalkClient]:
    """A started client on the device; on leaving it is stopped, and anything its loop raised fails the test."""
    client = SimulatedHopTalkClient(
        device,
        timing=timing,
        storage=storage,
        reads_messages_in_open_conversations=reads_messages_in_open_conversations,
    )
    client.start()
    try:
        yield client
    finally:
        await client.stop()
    assert client.internal_errors == [], "the simulated client raised internally"


@pytest.fixture
async def client(device: SimulatedDevice, relay: ScriptedRelay) -> AsyncIterator[SimulatedHopTalkClient]:
    async with running_client(device) as running:
        yield running


async def sign_in_as(client: SimulatedHopTalkClient, username: str, password: str = PASSWORD) -> None:
    """Sign in and wait until the "F *" that follows was answered, so it stays out of later counts."""
    sign_in = client.sign_in(username, password)
    await sign_in.wait_until_finished()
    assert sign_in.is_signed_in, sign_in
    await client.wait_until(
        lambda: not any(isinstance(request, ConversationRefresh) for request in client.unfinished_requests),
        description="the refresh after the sign-in to be answered",
    )


def sent_of_type(client: SimulatedHopTalkClient, message_type_letter: str) -> list[SentDirectMessage]:
    return [sent for sent in client.sent_direct_messages if sent.message_type_letter == message_type_letter]


def part_numbers_sent(client: SimulatedHopTalkClient) -> list[int]:
    part_numbers: list[int] = []
    for text in client.sent_texts(ClientMessageType.MESSAGE_PART_REQUEST):
        parsed_part = parse_direct_message_text(text)
        assert isinstance(parsed_part, MessagePartRequest)
        part_numbers.append(parsed_part.part_number)
    return part_numbers


async def wait_for_sent(client: SimulatedHopTalkClient, message_type_letter: str, count: int) -> None:
    await client.wait_until(
        lambda: len(client.sent_texts(message_type_letter)) >= count,
        description=f"{count} {message_type_letter} direct messages to be sent",
    )


async def wait_for_received(client: SimulatedHopTalkClient, count: int) -> None:
    await client.wait_until(
        lambda: len(client.received_direct_messages) >= count,
        description=f"{count} direct messages from the server",
    )


def device_knows_its_route_to_relay(device: SimulatedDevice) -> bool:
    relay_contact = device.stored_relay_contact
    return relay_contact is not None and relay_contact.has_known_route


async def wait_for_timer_to_pass(client: SimulatedHopTalkClient, seconds: float) -> None:
    """Let the client's own clock move on, to prove that nothing happens in that time."""
    deadline = client.clock.monotonic_seconds() + seconds
    await client.wait_until(lambda: client.clock.monotonic_seconds() >= deadline, timeout_seconds=seconds + 5)


# ----- sending: splitting, stored parts, rounds -----------------------------------------------


async def test_a_long_message_is_split_and_its_exact_parts_are_stored_before_the_first_part_is_sent(
    client: SimulatedHopTalkClient,
) -> None:
    await sign_in_as(client, "ivan")

    message = client.send_message("Bob", "a" * 250)
    parts_before_any_send = message.parts
    part_requests_before_any_send = client.sent_texts(ClientMessageType.MESSAGE_PART_REQUEST)
    await message.wait_for_status(OutgoingMessageStatus.SENT)

    assert [len(part.encode()) for part in parts_before_any_send] == [104, 104, 42]
    assert part_requests_before_any_send == []
    assert client.storage.outgoing_messages[message.key] is message
    assert client.sent_texts(ClientMessageType.MESSAGE_PART_REQUEST) == [
        format_message_part_request("Bob", message.message_id, part_number, 3, part_text)
        for part_number, part_text in enumerate(parts_before_any_send, start=1)
    ]


async def test_the_split_keeps_grapheme_clusters_whole_and_turns_carriage_returns_into_line_feeds(
    client: SimulatedHopTalkClient,
) -> None:
    await sign_in_as(client, "ivan")

    family_message = client.send_message("Bob", ZERO_WIDTH_JOINER_FAMILY * 5)
    line_message = client.send_message("Bob", "first\r\nsecond\rthird")
    await line_message.wait_for_status(OutgoingMessageStatus.SENT)

    assert family_message.parts == (ZERO_WIDTH_JOINER_FAMILY * 4, ZERO_WIDTH_JOINER_FAMILY)
    assert line_message.parts == ("first\nsecond\nthird",)


async def test_a_retried_part_is_byte_identical_and_goes_out_after_the_retry_pause_with_a_new_timestamp(
    client: SimulatedHopTalkClient, relay: ScriptedRelay
) -> None:
    await sign_in_as(client, "ivan")
    relay.leave_unanswered(ClientMessageType.MESSAGE_PART_REQUEST)

    message = client.send_message("Bob", "Привет, Боб!")
    await message.wait_for_status(OutgoingMessageStatus.SENT)

    first_copy, second_copy = sent_of_type(client, ClientMessageType.MESSAGE_PART_REQUEST)
    assert first_copy.text == second_copy.text
    assert second_copy.meshcore_timestamp > first_copy.meshcore_timestamp
    assert second_copy.handed_to_node_at - first_copy.handed_to_node_at >= FAST_TIMING.retry_pause_seconds(0)
    assert message.retry_rounds == 1


async def test_a_status_with_zeros_after_the_whole_round_brings_exactly_the_missing_parts_at_once(
    client: SimulatedHopTalkClient, relay: ScriptedRelay
) -> None:
    await sign_in_as(client, "ivan")
    first_copy_of_part_two_is_lost = True

    def lose_the_first_copy_of_part_two(text: str) -> list[str]:
        nonlocal first_copy_of_part_two_is_lost
        parsed_message = parse_direct_message_text(text)
        is_part_two = isinstance(parsed_message, MessagePartRequest) and parsed_message.part_number == 2
        if is_part_two and first_copy_of_part_two_is_lost:
            first_copy_of_part_two_is_lost = False
            return []
        return relay.answer_like_a_server(text)

    relay.answer = lose_the_first_copy_of_part_two

    message = client.send_message("Bob", "x" * 250)
    await message.wait_for_status(OutgoingMessageStatus.SENT)

    assert part_numbers_sent(client) == [1, 2, 3, 2]
    assert relay.sent_texts[-2:] == [
        format_send_status_reply("Bob", message.message_id, "101"),
        format_send_status_reply("Bob", message.message_id, "111"),
    ]
    assert client.counters.missing_part_resends == 1
    assert message.retry_rounds == 0


async def test_the_latest_status_replaces_the_confirmed_set_instead_of_adding_to_it(
    client: SimulatedHopTalkClient, relay: ScriptedRelay
) -> None:
    """A server that lost part 1 after reporting it says so, and only an all-ones status completes."""
    await sign_in_as(client, "ivan")
    statuses_by_received_part: dict[int, str] = {3: "110", 4: "011", 5: "111"}
    received_part_count = 0

    def report_scripted_statuses(text: str) -> list[str]:
        nonlocal received_part_count
        parsed_message = parse_direct_message_text(text)
        assert isinstance(parsed_message, MessagePartRequest)
        received_part_count += 1
        received_set = statuses_by_received_part.get(received_part_count)
        if received_set is None:
            return []
        return [format_send_status_reply("Bob", parsed_message.message_id, received_set)]

    relay.answer = report_scripted_statuses

    message = client.send_message("Bob", "y" * 250)
    await message.wait_for_status(OutgoingMessageStatus.SENT)

    assert part_numbers_sent(client) == [1, 2, 3, 3, 1]
    assert message.confirmed_set == "111"
    assert message.retry_rounds == 0


async def test_without_any_status_every_unconfirmed_part_is_sent_again_on_the_retry_schedule(
    client: SimulatedHopTalkClient, relay: ScriptedRelay
) -> None:
    await sign_in_as(client, "ivan")
    relay.leave_unanswered(ClientMessageType.MESSAGE_PART_REQUEST, count=6)

    message = client.send_message("Bob", "z" * 150)
    await message.wait_for_status(OutgoingMessageStatus.SENT)

    part_requests = sent_of_type(client, ClientMessageType.MESSAGE_PART_REQUEST)
    rounds = [part_requests[round_start : round_start + 2] for round_start in range(0, 8, 2)]
    assert part_numbers_sent(client) == [1, 2, 1, 2, 1, 2, 1, 2]
    for schedule_step, (previous_round, next_round) in enumerate(itertools.pairwise(rounds)):
        pause_seconds = next_round[0].handed_to_node_at - previous_round[-1].handed_to_node_at
        assert pause_seconds >= FAST_TIMING.retry_pause_seconds(schedule_step)
    assert message.retry_rounds == 3
    assert client.counters.retry_rounds_by_request_type[ClientMessageType.MESSAGE_PART_REQUEST] == 3


async def test_a_status_after_the_message_was_sent_changes_nothing(
    client: SimulatedHopTalkClient, relay: ScriptedRelay
) -> None:
    await sign_in_as(client, "ivan")
    message = client.send_message("Bob", "w" * 150)
    await message.wait_for_status(OutgoingMessageStatus.SENT)
    part_requests_when_sent = client.sent_texts(ClientMessageType.MESSAGE_PART_REQUEST)

    relay.send(format_send_status_reply("Bob", message.message_id, "10"))
    await client.wait_until(
        lambda: client.received_direct_messages[-1].handling is ReceivedDirectMessageHandling.UNMATCHED
    )
    await wait_for_timer_to_pass(client, FAST_TIMING.retry_pause_seconds(0))

    assert message.status is OutgoingMessageStatus.SENT
    assert message.confirmed_set == "11"
    assert client.sent_texts(ClientMessageType.MESSAGE_PART_REQUEST) == part_requests_when_sent


async def test_any_receipt_completes_a_message_whose_status_never_arrived(
    client: SimulatedHopTalkClient, relay: ScriptedRelay
) -> None:
    await sign_in_as(client, "ivan")
    relay.leave_unanswered(ClientMessageType.MESSAGE_PART_REQUEST, count=100)
    message = client.send_message("Bob", "delivered before any status")
    await wait_for_sent(client, ClientMessageType.MESSAGE_PART_REQUEST, 1)

    relay.send(format_receipt_push("Bob", message.message_id, ReceiptLevel.DELIVERED))
    await message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    await wait_for_sent(client, ClientMessageType.RECEIPT_ACKNOWLEDGEMENT, 1)
    await wait_for_timer_to_pass(client, FAST_TIMING.retry_pause_seconds(0) * 2)

    assert message.state is RequestState.ANSWERED
    assert len(client.sent_texts(ClientMessageType.MESSAGE_PART_REQUEST)) == 1
    assert client.sent_texts(ClientMessageType.RECEIPT_ACKNOWLEDGEMENT) == [f"HT1 C Bob {message.message_id} D"]


async def test_an_id_conflict_sends_the_message_again_under_a_new_id(
    client: SimulatedHopTalkClient, relay: ScriptedRelay
) -> None:
    await sign_in_as(client, "ivan")
    conflict_reported = False

    def report_one_id_conflict(text: str) -> list[str]:
        nonlocal conflict_reported
        parsed_message = parse_direct_message_text(text)
        if isinstance(parsed_message, MessagePartRequest) and not conflict_reported:
            conflict_reported = True
            reference = f"{parsed_message.recipient_username} {parsed_message.message_id}"
            return [format_error_reply(ErrorCode.ID_CONFLICT, ClientMessageType.MESSAGE_PART_REQUEST, reference)]
        return relay.answer_like_a_server(text)

    relay.answer = report_one_id_conflict

    message = client.send_message("Bob", "one id, two messages")
    first_message_id = message.message_id
    await message.wait_for_status(OutgoingMessageStatus.SENT)

    assert message.replaced_message_ids == [first_message_id]
    assert message.message_id > first_message_id
    assert client.sent_texts(ClientMessageType.MESSAGE_PART_REQUEST) == [
        format_message_part_request("Bob", first_message_id, 1, 1, "one id, two messages"),
        format_message_part_request("Bob", message.message_id, 1, 1, "one id, two messages"),
    ]
    assert client.counters.id_conflict_resends == 1
    assert client.outgoing_messages("Bob") == [message]


async def test_a_permanent_error_fails_the_message_and_stops_its_retries(client: SimulatedHopTalkClient) -> None:
    await sign_in_as(client, "ivan")

    message = client.send_message("carol", "is anybody there")
    await message.wait_for_status(OutgoingMessageStatus.FAILED)
    await wait_for_timer_to_pass(client, FAST_TIMING.retry_pause_seconds(0) * 2)

    assert message.failure_reason == ErrorCode.NO_SUCH_USER
    assert message.state is RequestState.FAILED
    assert len(client.sent_texts(ClientMessageType.MESSAGE_PART_REQUEST)) == 1


# ----- receiving -------------------------------------------------------------------------------


async def test_parts_out_of_order_and_duplicated_are_reassembled_and_the_message_is_displayed_once(
    device: SimulatedDevice, relay: ScriptedRelay
) -> None:
    patient_timing = dataclasses.replace(FAST_TIMING, incomplete_acknowledgement_coalescing_seconds=1.0)
    async with running_client(device, timing=patient_timing) as client:
        await sign_in_as(client, "Bob")
        received_before = len(client.received_direct_messages)
        parts = {1: "Привет, ", 2: "Боб", 3: "!"}

        for copy_count, part_number in enumerate([3, 1, 3, 2, 2], start=1):
            relay.send(format_delivery_part("ivan", INCOMING_MESSAGE_ID, part_number, 3, parts[part_number]))
            await wait_for_received(client, received_before + copy_count)
        await wait_for_sent(client, ClientMessageType.DELIVERY_ACKNOWLEDGEMENT, 2)

        [incoming_message] = client.received_messages("IVAN")
        assert incoming_message.text == "Привет, Боб!"
        assert client.displayed_messages == [incoming_message]
        assert incoming_message.part_copies_received == 5
        assert client.sent_texts(ClientMessageType.DELIVERY_ACKNOWLEDGEMENT) == [
            f"HT1 K ivan {INCOMING_MESSAGE_ID} 111",
            f"HT1 K ivan {INCOMING_MESSAGE_ID} 111",
        ]


async def test_an_incomplete_acknowledgement_waits_for_the_parts_to_pause_and_a_complete_one_goes_at_once(
    device: SimulatedDevice, relay: ScriptedRelay
) -> None:
    coalescing_timing = dataclasses.replace(FAST_TIMING, incomplete_acknowledgement_coalescing_seconds=0.2)
    async with running_client(device, timing=coalescing_timing) as client:
        await sign_in_as(client, "Bob")
        received_before = len(client.received_direct_messages)

        relay.send(format_delivery_part("ivan", INCOMING_MESSAGE_ID, 1, 3, "one "))
        relay.send(format_delivery_part("ivan", INCOMING_MESSAGE_ID, 2, 3, "two "))
        await wait_for_received(client, received_before + 2)
        last_part_received_at = client.received_direct_messages[-1].received_at
        await wait_for_sent(client, ClientMessageType.DELIVERY_ACKNOWLEDGEMENT, 1)
        [incomplete_acknowledgement] = sent_of_type(client, ClientMessageType.DELIVERY_ACKNOWLEDGEMENT)
        relay.send(format_delivery_part("ivan", INCOMING_MESSAGE_ID, 3, 3, "three"))
        await wait_for_sent(client, ClientMessageType.DELIVERY_ACKNOWLEDGEMENT, 2)
        complete_acknowledgement = sent_of_type(client, ClientMessageType.DELIVERY_ACKNOWLEDGEMENT)[1]
        third_part_received_at = client.received_direct_messages[-1].received_at

        assert incomplete_acknowledgement.text == f"HT1 K ivan {INCOMING_MESSAGE_ID} 110"
        assert incomplete_acknowledgement.handed_to_node_at - last_part_received_at >= 0.2
        assert complete_acknowledgement.text == f"HT1 K ivan {INCOMING_MESSAGE_ID} 111"
        assert complete_acknowledgement.handed_to_node_at - third_part_received_at < 0.2
        assert [message.text for message in client.received_messages("ivan")] == ["one two three"]


async def test_a_read_is_sent_once_and_retried_until_the_server_confirms_it(
    client: SimulatedHopTalkClient, relay: ScriptedRelay
) -> None:
    await sign_in_as(client, "Bob")
    relay.send(format_delivery_part("ivan", INCOMING_MESSAGE_ID, 1, 1, "read me"))
    await client.wait_until(lambda: len(client.displayed_messages) == 1)
    relay.leave_unanswered(ClientMessageType.READ_REQUEST)

    read_request = client.mark_read("ivan", INCOMING_MESSAGE_ID)
    await read_request.wait_until_finished()
    same_read_request = client.mark_read("ivan", INCOMING_MESSAGE_ID)

    assert read_request.state is RequestState.ANSWERED
    assert same_read_request is read_request
    assert client.sent_texts(ClientMessageType.READ_REQUEST) == [f"HT1 R ivan {INCOMING_MESSAGE_ID}"] * 2
    assert read_request.retry_rounds == 1


async def test_receipts_only_raise_the_status_and_every_one_is_confirmed_even_for_an_unknown_id(
    client: SimulatedHopTalkClient, relay: ScriptedRelay
) -> None:
    await sign_in_as(client, "ivan")
    message = client.send_message("Bob", "tell me when you read it")
    await message.wait_for_status(OutgoingMessageStatus.SENT)
    unknown_message_id = message.message_id + 1_000

    receipts = [
        (message.message_id, ReceiptLevel.READ),
        (message.message_id, ReceiptLevel.DELIVERED),
        (message.message_id, ReceiptLevel.READ),
        (unknown_message_id, ReceiptLevel.DELIVERED),
    ]
    for receipt_count, (message_id, receipt_level) in enumerate(receipts, start=1):
        relay.send(format_receipt_push("Bob", message_id, receipt_level))
        await wait_for_sent(client, ClientMessageType.RECEIPT_ACKNOWLEDGEMENT, receipt_count)

    assert message.status is OutgoingMessageStatus.READ
    assert client.sent_texts(ClientMessageType.RECEIPT_ACKNOWLEDGEMENT) == [
        f"HT1 C Bob {message_id} {receipt_level}" for message_id, receipt_level in receipts
    ]
    assert [receipt.message_id for receipt in client.receipts_for_unknown_messages] == [unknown_message_id]


# ----- refresh ---------------------------------------------------------------------------------


async def test_a_conversation_refresh_is_retried_only_while_its_conversation_stays_open(
    client: SimulatedHopTalkClient, relay: ScriptedRelay
) -> None:
    await sign_in_as(client, "Bob")
    relay.leave_unanswered(ClientMessageType.REFRESH_REQUEST, count=100)

    refresh = client.open_conversation("ivan")
    reopened_refresh = client.open_conversation("IVAN")
    await wait_for_sent(client, ClientMessageType.REFRESH_REQUEST, 3)
    client.close_conversation("ivan")
    await wait_for_timer_to_pass(client, FAST_TIMING.retry_pause_seconds(1) * 2)

    assert reopened_refresh is refresh
    assert refresh.state is RequestState.ABANDONED
    assert refresh.retry_rounds == 1
    assert client.sent_texts(ClientMessageType.REFRESH_REQUEST) == ["HT1 F *", "HT1 F ivan", "HT1 F ivan"]


async def test_opening_a_conversation_again_after_its_refresh_was_answered_sends_a_new_one(
    client: SimulatedHopTalkClient,
) -> None:
    await sign_in_as(client, "Bob")

    first_refresh = client.open_conversation("ivan")
    await first_refresh.wait_until_finished()
    second_refresh = client.open_conversation("ivan")
    await second_refresh.wait_until_finished()

    assert second_refresh is not first_refresh
    assert (first_refresh.reported_message_count, second_refresh.reported_message_count) == (0, 0)
    assert client.sent_texts(ClientMessageType.REFRESH_REQUEST) == ["HT1 F *", "HT1 F ivan", "HT1 F ivan"]


async def test_every_sign_in_refreshes_everything_and_retries_that_at_most_three_times(
    device: SimulatedDevice, relay: ScriptedRelay
) -> None:
    short_pauses_timing = dataclasses.replace(FAST_TIMING, retry_pauses_seconds=(0.05, 0.1, 0.15, 0.2))
    relay.leave_unanswered(ClientMessageType.REFRESH_REQUEST, count=100)
    async with running_client(device, timing=short_pauses_timing) as client:
        sign_in = client.sign_in("ivan", PASSWORD)
        await sign_in.wait_until_finished()
        await wait_for_sent(client, ClientMessageType.REFRESH_REQUEST, 1)
        [refresh_of_everything] = [
            request for request in client.unfinished_requests if isinstance(request, ConversationRefresh)
        ]
        await refresh_of_everything.wait_until_finished()

        assert refresh_of_everything.state is RequestState.ABANDONED
        assert refresh_of_everything.retry_rounds == 3
        assert client.sent_texts(ClientMessageType.REFRESH_REQUEST) == ["HT1 F *"] * 4


async def test_reconnecting_to_the_node_after_a_long_server_silence_refreshes_everything(
    device: SimulatedDevice, relay: ScriptedRelay
) -> None:
    short_silence_timing = dataclasses.replace(FAST_TIMING, server_silence_before_refresh_all_seconds=0.1)
    async with running_client(device, timing=short_silence_timing) as client:
        await sign_in_as(client, "ivan")
        device.phone_leaves()
        await wait_for_timer_to_pass(client, 0.15)
        device.phone_returns()
        await wait_for_sent(client, ClientMessageType.REFRESH_REQUEST, 2)

        assert client.counters.node_reconnections == 1
        assert client.sent_texts(ClientMessageType.REFRESH_REQUEST) == ["HT1 F *", "HT1 F *"]


# ----- sign-in and account switches ------------------------------------------------------------


async def test_a_user_check_matches_the_answer_case_insensitively(client: SimulatedHopTalkClient) -> None:
    await sign_in_as(client, "ivan")

    existing_user_query = client.check_user("BOB")
    missing_user_query = client.check_user("nobody")
    await existing_user_query.wait_until_finished()
    await missing_user_query.wait_until_finished()

    assert (existing_user_query.user_exists, existing_user_query.answered_username) == (True, "Bob")
    assert (missing_user_query.user_exists, missing_user_query.answered_username) == (False, "nobody")


async def test_server_pushes_are_discarded_unanswered_while_no_account_is_signed_in(
    client: SimulatedHopTalkClient, relay: ScriptedRelay
) -> None:
    relay.send(format_delivery_part("ivan", INCOMING_MESSAGE_ID, 1, 1, "for whom?"))
    relay.send(format_receipt_push("ivan", INCOMING_MESSAGE_ID, ReceiptLevel.DELIVERED))
    await wait_for_received(client, 2)

    assert [received.handling for received in client.received_direct_messages] == [
        ReceivedDirectMessageHandling.DISCARDED_WITHOUT_ACCOUNT
    ] * 2
    assert client.counters.delivery_acknowledgements_queued == 0
    assert client.counters.receipt_acknowledgements_queued == 0
    assert client.node_link.queued_direct_messages == []
    with pytest.raises(ClientNotSignedInError):
        client.send_message("Bob", "not signed in")


async def test_switching_accounts_sets_the_old_requests_aside_and_discards_them_after_the_new_sign_in(
    client: SimulatedHopTalkClient, relay: ScriptedRelay
) -> None:
    await sign_in_as(client, "ivan")
    relay.leave_unanswered(ClientMessageType.MESSAGE_PART_REQUEST, count=100)
    message = client.send_message("Bob", "sent as ivan")
    await wait_for_sent(client, ClientMessageType.MESSAGE_PART_REQUEST, 1)
    relay.leave_unanswered(ClientMessageType.ACCOUNT_REQUEST)

    switch = client.sign_in("petr", PASSWORD)
    await switch.wait_until_finished()
    await wait_for_sent(client, ClientMessageType.REFRESH_REQUEST, 2)

    account_requests = sent_of_type(client, ClientMessageType.ACCOUNT_REQUEST)
    part_requests = sent_of_type(client, ClientMessageType.MESSAGE_PART_REQUEST)
    assert switch.retry_rounds == 1
    assert client.signed_in_username == "petr"
    assert all(part.handed_to_node_at < account_requests[1].handed_to_node_at for part in part_requests)
    assert (message.state, message.status) == (RequestState.DISCARDED, OutgoingMessageStatus.FAILED)
    assert message.failure_reason == FAILURE_REASON_ACCOUNT_SWITCHED
    assert client.outgoing_messages() == []
    assert client.events_of_kind(ClientEventKind.CONVERSATIONS_CLEARED) != []


async def test_server_pushes_are_discarded_unanswered_while_an_account_switch_is_under_way(
    client: SimulatedHopTalkClient, relay: ScriptedRelay
) -> None:
    await sign_in_as(client, "ivan")
    relay.leave_unanswered(ClientMessageType.ACCOUNT_REQUEST, count=100)
    client.sign_in("petr", PASSWORD)
    received_before = len(client.received_direct_messages)

    relay.send(format_delivery_part("Bob", INCOMING_MESSAGE_ID, 1, 1, "for ivan or for petr?"))
    await wait_for_received(client, received_before + 1)

    assert client.is_switching_account
    assert client.received_direct_messages[-1].handling is ReceivedDirectMessageHandling.DISCARDED_WITHOUT_ACCOUNT
    assert client.sent_texts(ClientMessageType.DELIVERY_ACKNOWLEDGEMENT) == []


async def test_a_failed_switch_sends_the_set_aside_requests_again(
    client: SimulatedHopTalkClient, relay: ScriptedRelay
) -> None:
    await sign_in_as(client, "ivan")
    relay.add_user("petr", OTHER_PASSWORD)
    relay.leave_unanswered(ClientMessageType.MESSAGE_PART_REQUEST)
    message = client.send_message("Bob", "still ivan's")
    await wait_for_sent(client, ClientMessageType.MESSAGE_PART_REQUEST, 1)

    switch = client.sign_in("petr", PASSWORD)
    await switch.wait_until_finished()
    await message.wait_for_status(OutgoingMessageStatus.SENT)

    assert switch.error_code == ErrorCode.WRONG_PASSWORD
    assert client.signed_in_username == "ivan"
    assert len(client.sent_texts(ClientMessageType.MESSAGE_PART_REQUEST)) == 2
    assert client.events_of_kind(ClientEventKind.REQUESTS_RESUMED) != []


async def test_an_account_reply_nobody_asked_for_signs_the_client_out_and_in_again(
    client: SimulatedHopTalkClient, relay: ScriptedRelay
) -> None:
    await sign_in_as(client, "Bob")
    relay.send(format_delivery_part("ivan", INCOMING_MESSAGE_ID, 1, 1, "kept across the relink"))
    await client.wait_until(lambda: len(client.displayed_messages) == 1)

    relay.send(format_account_reply("ivan"))
    await client.wait_until(lambda: client.events_of_kind(ClientEventKind.SIGNED_OUT_BY_UNEXPECTED_ACCOUNT_REPLY) != [])
    await wait_for_sent(client, ClientMessageType.ACCOUNT_REQUEST, 2)
    await client.wait_until(lambda: client.signed_in_username == "Bob")
    await wait_for_sent(client, ClientMessageType.REFRESH_REQUEST, 2)

    assert client.sent_texts(ClientMessageType.ACCOUNT_REQUEST) == [f"HT1 A Bob {PASSWORD}"] * 2
    assert [message.text for message in client.received_messages("ivan")] == ["kept across the relink"]
    assert client.counters.unexpected_account_replies == 1


async def test_an_account_reply_after_an_error_for_the_latest_sign_in_signs_the_client_in(
    client: SimulatedHopTalkClient, relay: ScriptedRelay
) -> None:
    relay.answer = lambda text: [
        format_error_reply(ErrorCode.WRONG_PASSWORD, ClientMessageType.ACCOUNT_REQUEST, "ivan")
    ]
    sign_in = client.sign_in("ivan", PASSWORD)
    await sign_in.wait_until_finished()
    failed_with = sign_in.error_code

    relay.answer = relay.answer_like_a_server
    relay.send(format_account_reply("ivan"))
    await client.wait_until(lambda: client.signed_in_username == "ivan")

    assert failed_with == ErrorCode.WRONG_PASSWORD
    assert sign_in.state is RequestState.ANSWERED
    assert sign_in.was_answered_after_error
    await wait_for_sent(client, ClientMessageType.REFRESH_REQUEST, 1)


async def test_a_rate_limited_sign_in_stays_pending_and_is_sent_again_after_the_wait(
    device: SimulatedDevice, relay: ScriptedRelay
) -> None:
    rate_limit_timing = dataclasses.replace(FAST_TIMING, rate_limited_wait_seconds=0.3)
    rate_limited_once = False

    def rate_limit_the_first_sign_in(text: str) -> list[str]:
        nonlocal rate_limited_once
        if not rate_limited_once and message_type_letter_of(text) == ClientMessageType.ACCOUNT_REQUEST:
            rate_limited_once = True
            return [format_error_reply(ErrorCode.RATE_LIMITED, ClientMessageType.ACCOUNT_REQUEST, "ivan")]
        return relay.answer_like_a_server(text)

    relay.answer = rate_limit_the_first_sign_in
    async with running_client(device, timing=rate_limit_timing) as client:
        sign_in = client.sign_in("ivan", PASSWORD)
        await client.wait_until(lambda: sign_in.is_rate_limited)
        rate_limited_at = client.received_direct_messages[-1].received_at
        was_pending_while_rate_limited = sign_in.is_active
        await sign_in.wait_until_finished()

        first_copy, second_copy = sent_of_type(client, ClientMessageType.ACCOUNT_REQUEST)
        assert was_pending_while_rate_limited
        assert sign_in.is_signed_in
        assert second_copy.handed_to_node_at - rate_limited_at >= 0.3
        assert second_copy.text == first_copy.text


async def test_a_new_sign_in_after_a_wrong_password_waits_before_it_is_sent(
    device: SimulatedDevice, relay: ScriptedRelay
) -> None:
    wrong_password_timing = dataclasses.replace(FAST_TIMING, wrong_password_wait_seconds=0.3)
    async with running_client(device, timing=wrong_password_timing) as client:
        wrong_sign_in = client.sign_in("ivan", OTHER_PASSWORD)
        await wrong_sign_in.wait_until_finished()
        right_sign_in = client.sign_in("ivan", PASSWORD)
        await right_sign_in.wait_until_finished()

        wrong_password_answered_at = client.storage.wrong_password_times["ivan"]
        second_account_request = sent_of_type(client, ClientMessageType.ACCOUNT_REQUEST)[1]
        assert wrong_sign_in.error_code == ErrorCode.WRONG_PASSWORD
        assert right_sign_in.is_signed_in
        assert second_account_request.handed_to_node_at - wrong_password_answered_at >= 0.3


async def test_not_signed_in_signs_the_client_out_and_sets_its_requests_aside(
    client: SimulatedHopTalkClient, relay: ScriptedRelay
) -> None:
    await sign_in_as(client, "ivan")
    relay.leave_unanswered(ClientMessageType.MESSAGE_PART_REQUEST, count=100)
    message = client.send_message("Bob", "waits for ivan")
    await wait_for_sent(client, ClientMessageType.MESSAGE_PART_REQUEST, 1)
    relay.answer = lambda text: [format_error_reply(ErrorCode.NOT_SIGNED_IN, ClientMessageType.QUERY_REQUEST, "Bob")]

    query = client.check_user("Bob")
    await query.wait_until_finished()

    assert query.error_code == ErrorCode.NOT_SIGNED_IN
    assert not client.is_signed_in
    assert message.state is RequestState.SET_ASIDE
    assert client.events_of_kind(ClientEventKind.SIGNED_OUT_BY_SERVER) != []


# ----- restart, pinning and what the client ignores -------------------------------------------


async def test_a_restarted_client_resumes_its_pending_requests_from_its_storage(
    device: SimulatedDevice, relay: ScriptedRelay
) -> None:
    storage = SimulatedClientStorage()
    relay.leave_unanswered(ClientMessageType.MESSAGE_PART_REQUEST)
    async with running_client(device, storage=storage) as first_app:
        await sign_in_as(first_app, "ivan")
        message = first_app.send_message("Bob", "survives a restart")
        await wait_for_sent(first_app, ClientMessageType.MESSAGE_PART_REQUEST, 1)
        first_app_part_request = first_app.sent_texts(ClientMessageType.MESSAGE_PART_REQUEST)
        last_timestamp_of_first_app = first_app.sent_direct_messages[-1].meshcore_timestamp

    async with running_client(device, storage=storage) as restarted_app:
        await message.wait_for_status(OutgoingMessageStatus.SENT)

        assert restarted_app.signed_in_username == "ivan"
        assert restarted_app.sent_texts(ClientMessageType.MESSAGE_PART_REQUEST) == first_app_part_request
        assert restarted_app.sent_direct_messages[0].meshcore_timestamp > last_timestamp_of_first_app


async def test_meshcore_timestamps_and_message_ids_strictly_increase_even_when_the_wall_clock_steps_back(
    device: SimulatedDevice, relay: ScriptedRelay
) -> None:
    class SteppingClock:
        def __init__(self) -> None:
            self.wall_clock_offset_seconds = 0.0

        def monotonic_seconds(self) -> float:
            return time.monotonic()

        def wall_clock_seconds(self) -> float:
            return 1_790_294_400.0 + self.wall_clock_offset_seconds

    stepping_clock = SteppingClock()
    client = SimulatedHopTalkClient(device, timing=FAST_TIMING, clock=stepping_clock)
    async with client:
        await sign_in_as(client, "ivan")
        messages_before_the_step = [client.send_message("Bob", f"before {number}") for number in range(3)]
        await messages_before_the_step[-1].wait_for_status(OutgoingMessageStatus.SENT)
        stepping_clock.wall_clock_offset_seconds = -3600.0
        messages_after_the_step = [client.send_message("Bob", f"after {number}") for number in range(3)]
        await messages_after_the_step[-1].wait_for_status(OutgoingMessageStatus.SENT)

    meshcore_timestamps = [sent.meshcore_timestamp for sent in client.sent_direct_messages]
    message_ids = [message.message_id for message in [*messages_before_the_step, *messages_after_the_step]]
    assert all(earlier < later for earlier, later in itertools.pairwise(meshcore_timestamps))
    assert all(earlier < later for earlier, later in itertools.pairwise(message_ids))
    assert client.internal_errors == []


async def test_a_reinstalled_app_sharing_the_scaled_clock_never_reuses_a_meshcore_timestamp(
    device: SimulatedDevice, relay: ScriptedRelay
) -> None:
    scaled_clock = ScaledClock(TEST_TIME_FACTOR)
    first_install = SimulatedHopTalkClient(device, timing=FAST_TIMING, clock=scaled_clock)
    async with first_install:
        await sign_in_as(first_install, "ivan")
        sent_messages = [first_install.send_message("Bob", f"number {number}") for number in range(5)]
        await sent_messages[-1].wait_for_status(OutgoingMessageStatus.SENT)
    reinstalled_app = SimulatedHopTalkClient(device, timing=FAST_TIMING, clock=scaled_clock)
    async with reinstalled_app:
        await sign_in_as(reinstalled_app, "ivan")

    last_timestamp_of_first_install = first_install.sent_direct_messages[-1].meshcore_timestamp
    assert reinstalled_app.sent_direct_messages[0].meshcore_timestamp > last_timestamp_of_first_install
    assert first_install.internal_errors == reinstalled_app.internal_errors == []


async def test_other_text_and_malformed_or_unknown_server_messages_are_never_acted_on(
    client: SimulatedHopTalkClient, relay: ScriptedRelay
) -> None:
    await sign_in_as(client, "ivan")
    received_before = len(client.received_direct_messages)
    sent_before = len(client.sent_direct_messages)
    ignored_texts = ["hello there", "HT1 k Bob 01 1", "HT1 z something newer", "HT2 m ivan 5 1/1 from the future"]

    for text in ignored_texts:
        relay.send(text)
    await wait_for_received(client, received_before + len(ignored_texts))

    assert [received.handling for received in client.received_direct_messages[received_before:]] == [
        ReceivedDirectMessageHandling.OTHER_TEXT,
        ReceivedDirectMessageHandling.DROPPED_MALFORMED,
        ReceivedDirectMessageHandling.IGNORED,
        ReceivedDirectMessageHandling.IGNORED,
    ]
    assert len(client.sent_direct_messages) == sent_before
    assert client.node_link.queued_direct_messages == []


async def test_messages_wait_for_the_phone_to_come_back_before_the_retry_timer_starts(
    client: SimulatedHopTalkClient, device: SimulatedDevice
) -> None:
    await sign_in_as(client, "ivan")
    device.phone_leaves()

    message = client.send_message("Bob", "written while away")
    await wait_for_timer_to_pass(client, FAST_TIMING.retry_pause_seconds(0) * 2)
    sent_while_away = client.sent_texts(ClientMessageType.MESSAGE_PART_REQUEST)
    device.phone_returns()
    await message.wait_for_status(OutgoingMessageStatus.SENT)

    assert sent_while_away == []
    assert message.retry_rounds == 0
    assert len(client.sent_texts(ClientMessageType.MESSAGE_PART_REQUEST)) == 1


async def test_a_message_displayed_in_an_open_conversation_is_marked_read(
    device: SimulatedDevice, relay: ScriptedRelay
) -> None:
    async with running_client(device, reads_messages_in_open_conversations=True) as client:
        await sign_in_as(client, "Bob")
        client.open_conversation("ivan")

        relay.send(format_delivery_part("ivan", INCOMING_MESSAGE_ID, 1, 1, "seen at once"))
        [incoming_message] = await client.wait_for_received_messages("ivan", 1)
        await client.wait_until(lambda: client.is_idle)

        assert incoming_message.read_request is not None
        assert incoming_message.read_request.state is RequestState.ANSWERED
        assert client.sent_texts(ClientMessageType.READ_REQUEST) == [f"HT1 R ivan {INCOMING_MESSAGE_ID}"]


# ----- pacing on the device's own node --------------------------------------------------------


async def test_the_client_leaves_the_minimum_gap_between_two_direct_messages(
    device: SimulatedDevice, relay: ScriptedRelay
) -> None:
    gap_timing = dataclasses.replace(FAST_TIMING, minimum_gap_between_direct_messages_seconds=0.05)
    async with running_client(device, timing=gap_timing) as client:
        await sign_in_as(client, "ivan")
        message = client.send_message("Bob", "g" * 500)
        await message.wait_for_status(OutgoingMessageStatus.SENT)

        hand_off_times = [sent.handed_to_node_at for sent in client.sent_direct_messages]
        assert len(sent_of_type(client, ClientMessageType.MESSAGE_PART_REQUEST)) == 5
        assert all(later - earlier >= 0.05 for earlier, later in itertools.pairwise(hand_off_times))


async def test_at_most_four_direct_messages_wait_for_a_firmware_acknowledgement(
    device: SimulatedDevice, relay: ScriptedRelay
) -> None:
    device.downlink = LinkPolicy(acknowledgement_loss_probability=1.0)
    no_gap_timing = dataclasses.replace(
        FAST_TIMING, minimum_gap_between_direct_messages_seconds=0.0, minimum_acknowledgement_wait_seconds=0.2
    )
    async with running_client(device, timing=no_gap_timing) as client:
        await sign_in_as(client, "ivan")
        message = client.send_message("Bob", "p" * 1000)
        await message.wait_for_status(OutgoingMessageStatus.SENT, timeout_seconds=10)

        sent_direct_messages = client.sent_direct_messages
        for sent_direct_message in sent_direct_messages:
            still_waiting = [
                other
                for other in sent_direct_messages
                if other is not sent_direct_message
                and other.handed_to_node_at <= sent_direct_message.handed_to_node_at < other.stopped_waiting_at
            ]
            assert len(still_waiting) < 4
        assert max(sent.awaiting_acknowledgement_after_hand_off for sent in sent_direct_messages) == 4


async def test_a_full_packet_pool_makes_the_client_send_the_same_direct_message_again_with_a_new_timestamp(
    client: SimulatedHopTalkClient, device: SimulatedDevice
) -> None:
    await sign_in_as(client, "ivan")
    device.firmware.occupy_packet_pool(16)

    query = client.check_user("Bob")
    await client.wait_until(lambda: len(client.refused_hand_offs) >= 3)
    device.firmware.release_packet_pool()
    await query.wait_until_finished()

    refused_attempts = client.refused_hand_offs
    [accepted_query] = sent_of_type(client, ClientMessageType.QUERY_REQUEST)
    attempt_timestamps = [attempt.meshcore_timestamp for attempt in refused_attempts]
    attempt_times = [attempt.attempted_at for attempt in refused_attempts]
    assert {attempt.text for attempt in refused_attempts} == {accepted_query.text}
    assert attempt_timestamps == sorted(set(attempt_timestamps))
    assert accepted_query.meshcore_timestamp > attempt_timestamps[-1]
    assert all(
        later - earlier >= FAST_TIMING.table_full_wait_seconds for earlier, later in itertools.pairwise(attempt_times)
    )
    assert query.retry_rounds == 0
    assert client.counters.table_full_rejections == len(refused_attempts)


# ----- route hygiene on the device's own node -------------------------------------------------


async def test_a_server_message_that_arrived_by_flood_resets_the_route_before_it_is_answered(
    device: SimulatedDevice, relay: ScriptedRelay
) -> None:
    no_path_update_window_timing = dataclasses.replace(FAST_TIMING, path_update_window_seconds=0.0)
    async with running_client(device, timing=no_path_update_window_timing) as client:
        await sign_in_as(client, "Bob")

        relay.send(format_delivery_part("ivan", INCOMING_MESSAGE_ID, 1, 1, "by flood"), by_flood=True)
        await wait_for_sent(client, ClientMessageType.DELIVERY_ACKNOWLEDGEMENT, 1)

        flood_decisions = [
            decision
            for decision in client.route_hygiene_decisions
            if decision.trigger is RouteHygieneTrigger.FLOOD_ARRIVAL and "by flood" in decision.direct_message_text
        ]
        [delivery_acknowledgement] = sent_of_type(client, ClientMessageType.DELIVERY_ACKNOWLEDGEMENT)
        assert [decision.outcome for decision in flood_decisions] == [RouteHygieneOutcome.RESET]
        assert delivery_acknowledgement.sent_by_flood


async def test_a_recent_path_update_spares_the_reset_before_answering_a_flood(
    device: SimulatedDevice, relay: ScriptedRelay
) -> None:
    long_path_update_window_timing = dataclasses.replace(FAST_TIMING, path_update_window_seconds=5.0)
    async with running_client(device, timing=long_path_update_window_timing) as client:
        await sign_in_as(client, "Bob")
        await client.wait_until(lambda: client.node_link.last_path_update_seen_at is not None)
        resets_before = client.counters.route_resets

        relay.send(format_delivery_part("ivan", INCOMING_MESSAGE_ID, 1, 1, "by flood"), by_flood=True)
        await wait_for_sent(client, ClientMessageType.DELIVERY_ACKNOWLEDGEMENT, 1)

        [delivery_acknowledgement] = sent_of_type(client, ClientMessageType.DELIVERY_ACKNOWLEDGEMENT)
        assert client.route_hygiene_decisions[-1].outcome is RouteHygieneOutcome.SKIPPED_RECENT_PATH_UPDATE
        assert client.counters.route_resets == resets_before
        assert not delivery_acknowledgement.sent_by_flood


async def test_a_direct_message_without_a_firmware_acknowledgement_resets_the_route_so_the_retry_floods(
    client: SimulatedHopTalkClient, device: SimulatedDevice
) -> None:
    await sign_in_as(client, "ivan")
    await client.wait_until(lambda: device_knows_its_route_to_relay(device))
    device.change_route_to_relay(2)

    query = client.check_user("Bob")
    await query.wait_until_finished()

    first_copy, second_copy = sent_of_type(client, ClientMessageType.QUERY_REQUEST)[:2]
    missing_acknowledgement_decisions = [
        decision
        for decision in client.route_hygiene_decisions
        if decision.trigger is RouteHygieneTrigger.MISSING_ACKNOWLEDGEMENT
        and decision.direct_message_text == first_copy.text
    ]
    assert (first_copy.sent_by_flood, second_copy.sent_by_flood) == (False, True)
    assert missing_acknowledgement_decisions[0].outcome is RouteHygieneOutcome.RESET
    assert query.user_exists


async def test_a_lost_firmware_acknowledgement_keeps_the_route_when_the_server_answered(
    device: SimulatedDevice, relay: ScriptedRelay
) -> None:
    patient_acknowledgement_timing = dataclasses.replace(FAST_TIMING, minimum_acknowledgement_wait_seconds=0.5)
    async with running_client(device, timing=patient_acknowledgement_timing) as client:
        await sign_in_as(client, "ivan")
        await client.wait_until(lambda: device_knows_its_route_to_relay(device))
        device.downlink = LinkPolicy(acknowledgement_loss_probability=1.0)
        resets_before = client.counters.route_resets

        query = client.check_user("Bob")
        await query.wait_until_finished()
        [query_request] = sent_of_type(client, ClientMessageType.QUERY_REQUEST)
        await client.wait_until(lambda: query_request.acknowledgement_deadline_passed)

        [decision] = [
            decision
            for decision in client.route_hygiene_decisions
            if decision.direct_message_text == query_request.text
        ]
        assert not query_request.sent_by_flood
        assert decision.outcome is RouteHygieneOutcome.SKIPPED_SERVER_ANSWERED
        assert client.counters.route_resets == resets_before
