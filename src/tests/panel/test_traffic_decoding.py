import pytest

from panel.traffic_decoding import decode_direct_message_text, shorten_message_id


@pytest.mark.parametrize(
    ("direct_message_text", "contact_username", "expected_meaning"),
    [
        pytest.param("HT1 A ivan ********", "", "sign in as ivan", id="account request"),
        pytest.param("HT1 Q bob", "ivan", "does bob exist?", id="query request"),
        pytest.param(
            "HT1 M Bob 1790294400123457 2/3 Привет",
            "ivan",
            "message part 2/3 of ivan → Bob #…457",
            id="message part request",
        ),
        pytest.param(
            "HT1 M Bob 1790294400123457 1/1 hi", "", "message part 1/1 → Bob #…457", id="part from an unlinked device"
        ),
        pytest.param(
            "HT1 K ivan 1790294400123457 101",
            "bob",
            "delivery acknowledgement 101 for ivan #…457",
            id="delivery acknowledgement",
        ),
        pytest.param("HT1 R ivan 1790294400123457", "bob", "read ivan #…457", id="read request"),
        pytest.param(
            "HT1 C Bob 1790294400123457 R",
            "ivan",
            "receipt acknowledgement (read) for Bob #…457",
            id="receipt acknowledgement",
        ),
        pytest.param("HT1 F ivan", "bob", "refresh of ivan", id="refresh request"),
        pytest.param("HT1 F *", "bob", "refresh of every conversation", id="refresh of every peer"),
        pytest.param("HT1 a ivan", "ivan", "signed in as ivan", id="account reply"),
        pytest.param("HT1 q Bob 1", "ivan", "Bob exists", id="query reply for an existing user"),
        pytest.param("HT1 q carol 0", "ivan", "carol does not exist", id="query reply for a missing user"),
        pytest.param("HT1 k Bob 1790294400123457 101", "ivan", "send status 101 for Bob #…457", id="send status reply"),
        pytest.param(
            "HT1 m ivan 1790294400123457 2/3 Привет",
            "Bob",
            "message part 2/3 of ivan → Bob #…457",
            id="delivery part",
        ),
        pytest.param("HT1 r ivan 1790294400123457", "Bob", "read accepted for ivan #…457", id="read reply"),
        pytest.param("HT1 s Bob 1790294400123457 D", "ivan", "receipt: Bob #…457 was delivered", id="receipt push"),
        pytest.param("HT1 f ivan 3", "Bob", "refresh of ivan: 3 messages follow", id="refresh reply"),
        pytest.param("HT1 f * 1", "Bob", "refresh of every conversation: 1 message follows", id="refresh reply of all"),
        pytest.param(
            "HT1 e NO_SUCH_USER M carol 1790294400123457",
            "ivan",
            "error NO_SUCH_USER answering M carol 1790294400123457",
            id="error reply",
        ),
        pytest.param("HT1 e SYNTAX ?", "ivan", "error SYNTAX answering ?", id="error reply without a reference"),
        pytest.param("Hello, is anybody there?", "ivan", "not protocol", id="other text"),
        pytest.param("HT2 Q bob", "ivan", "protocol version 2, not supported", id="unsupported version"),
        pytest.param("HT1 X something", "ivan", "unknown message type X", id="unknown type"),
        pytest.param(
            "HT1 M Bob 1790294400123457 x/3 hi", "ivan", "syntax error in M Bob 1790294400123457", id="syntax"
        ),
    ],
)
def test_every_message_type_gets_its_own_phrase(
    direct_message_text: str, contact_username: str, expected_meaning: str
) -> None:
    assert decode_direct_message_text(direct_message_text, contact_username) == expected_meaning


def test_message_ids_are_shortened_to_their_last_digits() -> None:
    assert shorten_message_id(1790294400123457) == "#…457"
    assert shorten_message_id(42) == "#42"
