"""The reply queue's rules, on explicit times."""

from messaging.request_processing import ReplyReadiness
from worker.reply_queue import ReplyQueue
from worker.worker_timing import WorkerTiming

TIMING = WorkerTiming()
CONTACT_ID = 11
OTHER_CONTACT_ID = 12
REPLY_KEY = "m:bob:1"
START = 1000.0


def add_incomplete_status(reply_queue: ReplyQueue, text: str, now: float, reply_key: str = REPLY_KEY) -> None:
    reply_queue.add_reply(
        contact_id=CONTACT_ID,
        reply_key=reply_key,
        text=text,
        readiness=ReplyReadiness.COALESCED_INCOMPLETE_STATUS,
        now=now,
    )


def add_immediate_reply(
    reply_queue: ReplyQueue, text: str, now: float, *, reply_key: str = REPLY_KEY, contact_id: int = CONTACT_ID
) -> None:
    reply_queue.add_reply(
        contact_id=contact_id, reply_key=reply_key, text=text, readiness=ReplyReadiness.IMMEDIATE, now=now
    )


def take_texts(reply_queue: ReplyQueue, now: float) -> list[str]:
    texts: list[str] = []
    while (pending_reply := reply_queue.take_next_ready_reply(now)) is not None:
        texts.append(pending_reply.text)
    return texts


def send_immediate_reply(
    reply_queue: ReplyQueue, text: str, now: float, *, reply_key: str = REPLY_KEY, contact_id: int = CONTACT_ID
) -> None:
    """A reply queued, taken by the sender loop and sent at once."""
    add_immediate_reply(reply_queue, text, now, reply_key=reply_key, contact_id=contact_id)
    taken_reply = reply_queue.take_next_ready_reply(now)
    assert taken_reply is not None
    reply_queue.record_reply_sent(taken_reply, now)


def test_a_newer_reply_to_the_same_request_replaces_the_older_one() -> None:
    reply_queue = ReplyQueue(TIMING)
    add_immediate_reply(reply_queue, "HT1 e WRONG_PASSWORD A bob", START)
    add_immediate_reply(reply_queue, "HT1 a bob", START + 1)

    assert take_texts(reply_queue, START + 1) == ["HT1 a bob"]


def test_an_incomplete_status_goes_out_once_five_seconds_pass_without_a_new_part_however_long_the_burst() -> None:
    reply_queue = ReplyQueue(TIMING)
    part_count = 10
    for received_part_count in range(1, part_count + 1):
        part_time = START + 4 * received_part_count
        assert take_texts(reply_queue, part_time) == []
        received_set = "1" * received_part_count + "0" * (part_count - received_part_count)
        add_incomplete_status(reply_queue, f"HT1 k bob 1 {received_set}", part_time)

    last_part_time = START + 4 * part_count
    assert take_texts(reply_queue, last_part_time + 4.9) == []
    assert take_texts(reply_queue, last_part_time + 5) == ["HT1 k bob 1 1111111111"]


def test_a_complete_status_replaces_a_queued_incomplete_one_and_is_ready_at_once() -> None:
    reply_queue = ReplyQueue(TIMING)
    add_incomplete_status(reply_queue, "HT1 k bob 1 110", START)
    add_immediate_reply(reply_queue, "HT1 k bob 1 111", START + 1)

    assert take_texts(reply_queue, START + 1) == ["HT1 k bob 1 111"]


def test_a_reply_expires_a_minute_after_it_was_created() -> None:
    reply_queue = ReplyQueue(TIMING)
    add_immediate_reply(reply_queue, "HT1 q bob 1", START)

    assert take_texts(reply_queue, START + 60) == []
    assert len(reply_queue) == 0


def test_an_identical_text_to_the_same_contact_within_ten_seconds_is_dropped() -> None:
    reply_queue = ReplyQueue(TIMING)
    send_immediate_reply(reply_queue, "HT1 k bob 1 111", START)

    add_immediate_reply(reply_queue, "HT1 k bob 1 111", START + 9)
    assert take_texts(reply_queue, START + 9) == []

    add_immediate_reply(reply_queue, "HT1 k bob 1 111", START + 10)
    assert take_texts(reply_queue, START + 10) == ["HT1 k bob 1 111"]


def test_the_same_text_to_another_contact_is_not_dropped() -> None:
    reply_queue = ReplyQueue(TIMING)
    send_immediate_reply(reply_queue, "HT1 a bob", START, reply_key="a:bob")

    add_immediate_reply(reply_queue, "HT1 a bob", START + 1, contact_id=OTHER_CONTACT_ID)

    assert take_texts(reply_queue, START + 1) == ["HT1 a bob"]


def test_the_trackers_flood_resend_passes_four_seconds_after_the_first_copy() -> None:
    reply_queue = ReplyQueue(TIMING)
    send_immediate_reply(reply_queue, "HT1 a bob", START, reply_key="a:bob")

    reply_queue.add_flood_resend(contact_id=CONTACT_ID, reply_key="a:bob", text="HT1 a bob", now=START + 4)

    assert take_texts(reply_queue, START + 4) == ["HT1 a bob"]


def test_the_answer_to_a_retry_twenty_seconds_after_the_first_copy_goes_out_after_a_late_flood_resend() -> None:
    """The first copy's firmware ACK wait on a long route ended thirteen seconds after it was sent."""
    reply_queue = ReplyQueue(TIMING)
    wrong_password_error = "HT1 e WRONG_PASSWORD A bob"
    send_immediate_reply(reply_queue, wrong_password_error, START, reply_key="a:bob")
    reply_queue.add_flood_resend(contact_id=CONTACT_ID, reply_key="a:bob", text=wrong_password_error, now=START + 13)
    flood_resend = reply_queue.take_next_ready_reply(START + 13)
    assert flood_resend is not None
    reply_queue.record_reply_sent(flood_resend, START + 13)

    add_immediate_reply(reply_queue, wrong_password_error, START + 20, reply_key="a:bob")

    assert take_texts(reply_queue, START + 20) == [wrong_password_error]


def test_ready_replies_go_by_ready_time_then_in_the_order_they_were_queued() -> None:
    reply_queue = ReplyQueue(TIMING)
    add_incomplete_status(reply_queue, "HT1 k bob 1 10", START, reply_key="m:bob:1")
    add_immediate_reply(reply_queue, "HT1 q carol 1", START + 1, reply_key="q:carol")
    add_immediate_reply(reply_queue, "HT1 q dave 1", START + 1, reply_key="q:dave")

    assert take_texts(reply_queue, START + 10) == ["HT1 q carol 1", "HT1 q dave 1", "HT1 k bob 1 10"]


def test_the_next_ready_time_is_the_earliest_waiting_reply() -> None:
    reply_queue = ReplyQueue(TIMING)
    assert reply_queue.next_ready_time(START) is None

    add_incomplete_status(reply_queue, "HT1 k bob 1 10", START)

    assert reply_queue.next_ready_time(START) == START + TIMING.incomplete_status_coalescing_seconds


def test_a_reply_put_back_after_a_full_packet_pool_waits_unless_a_newer_one_replaced_it() -> None:
    reply_queue = ReplyQueue(TIMING)
    add_immediate_reply(reply_queue, "HT1 a bob", START)
    taken_reply = reply_queue.take_next_ready_reply(START)
    assert taken_reply is not None

    assert reply_queue.put_back(taken_reply, ready_at=START + 5)
    assert take_texts(reply_queue, START + 4) == []
    assert take_texts(reply_queue, START + 5) == ["HT1 a bob"]

    add_immediate_reply(reply_queue, "HT1 e RATE_LIMITED A bob", START + 6)
    assert not reply_queue.put_back(taken_reply, ready_at=START + 7)


def test_replies_to_deleted_contacts_are_dropped() -> None:
    reply_queue = ReplyQueue(TIMING)
    add_immediate_reply(reply_queue, "HT1 a bob", START, contact_id=CONTACT_ID)
    add_immediate_reply(reply_queue, "HT1 a carol", START, contact_id=OTHER_CONTACT_ID, reply_key="a:carol")

    assert reply_queue.drop_replies_to_contacts_other_than({OTHER_CONTACT_ID}) == 1
    assert take_texts(reply_queue, START) == ["HT1 a carol"]
