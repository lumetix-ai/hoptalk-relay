import struct

import pytest

from node.contact_cards import (
    InvalidContactCardError,
    MeshCoreNodeType,
    describe_node_type,
    parse_contact_card_uri,
)
from tests.services.node.node_builders import (
    SAMPLE_CONTACT_CARD_NAME,
    SAMPLE_CONTACT_CARD_PUBLIC_KEY,
    SAMPLE_CONTACT_CARD_TIMESTAMP,
    SAMPLE_CONTACT_CARD_URI,
    ContactCardSigner,
)

ROUTE_TYPE_TRANSPORT_FLOOD_HEADER = 0x10
ROUTE_TYPE_TRANSPORT_DIRECT_HEADER = 0x13
TEXT_MESSAGE_HEADER = 0x09
SIGNATURE_START_IN_SAMPLE_CARD = 2 + 32 + 4


def test_the_sample_card_of_meshcore_js_parses_and_its_signature_verifies() -> None:
    contact_card = parse_contact_card_uri(SAMPLE_CONTACT_CARD_URI)

    assert contact_card.public_key == SAMPLE_CONTACT_CARD_PUBLIC_KEY
    assert contact_card.advert_timestamp == SAMPLE_CONTACT_CARD_TIMESTAMP
    assert contact_card.node_type == MeshCoreNodeType.CHAT
    assert contact_card.name == SAMPLE_CONTACT_CARD_NAME
    assert (contact_card.latitude_microdegrees, contact_card.longitude_microdegrees) == (0, 0)
    assert contact_card.card_uri == SAMPLE_CONTACT_CARD_URI


def test_a_card_pasted_in_upper_case_across_lines_parses_and_is_kept_without_whitespace() -> None:
    card_hex = SAMPLE_CONTACT_CARD_URI.removeprefix("meshcore://").upper()
    pasted_card = f"  MESHCORE://{card_hex[:80]}\n{card_hex[80:]}  "

    contact_card = parse_contact_card_uri(pasted_card)

    assert contact_card.public_key == SAMPLE_CONTACT_CARD_PUBLIC_KEY
    assert " " not in contact_card.card_uri
    assert "\n" not in contact_card.card_uri


def test_a_flipped_signature_byte_is_refused() -> None:
    card_bytes = bytearray.fromhex(SAMPLE_CONTACT_CARD_URI.removeprefix("meshcore://"))
    card_bytes[SIGNATURE_START_IN_SAMPLE_CARD + 10] ^= 0x01

    with pytest.raises(InvalidContactCardError, match="signature does not verify"):
        parse_contact_card_uri("meshcore://" + card_bytes.hex())


def test_a_changed_name_breaks_the_signature() -> None:
    card_bytes = bytes.fromhex(SAMPLE_CONTACT_CARD_URI.removeprefix("meshcore://"))
    tampered_card = card_bytes.replace(b"Liam", b"Mary")

    with pytest.raises(InvalidContactCardError, match="signature does not verify"):
        parse_contact_card_uri("meshcore://" + tampered_card.hex())


def test_a_packet_that_is_not_an_advert_is_refused() -> None:
    contact_card_signer = ContactCardSigner()
    card_bytes = contact_card_signer.build_card_bytes(contact_card_signer.build_app_data(), header=TEXT_MESSAGE_HEADER)

    with pytest.raises(InvalidContactCardError, match="not an advert"):
        parse_contact_card_uri("meshcore://" + card_bytes.hex())


def test_an_advert_of_another_payload_version_is_refused() -> None:
    contact_card_signer = ContactCardSigner()
    card_bytes = contact_card_signer.build_card_bytes(contact_card_signer.build_app_data(), header=0x51)

    with pytest.raises(InvalidContactCardError, match="payload version 1"):
        parse_contact_card_uri("meshcore://" + card_bytes.hex())


@pytest.mark.parametrize("header", [ROUTE_TYPE_TRANSPORT_FLOOD_HEADER, ROUTE_TYPE_TRANSPORT_DIRECT_HEADER])
def test_the_transport_codes_of_the_transport_route_types_are_skipped(header: int) -> None:
    contact_card_signer = ContactCardSigner()
    card_bytes = contact_card_signer.build_card_bytes(
        contact_card_signer.build_app_data(name="Transport"),
        header=header,
        bytes_before_path_length=b"\x01\x02\x03\x04",
    )

    contact_card = parse_contact_card_uri("meshcore://" + card_bytes.hex())

    assert contact_card.public_key == contact_card_signer.public_key
    assert contact_card.name == "Transport"


@pytest.mark.parametrize(
    ("path_length_byte", "path_size"),
    [
        pytest.param(0x03, 3, id="three one-byte hashes"),
        pytest.param(0x42, 4, id="two two-byte hashes"),
        pytest.param(0x81, 3, id="one three-byte hash"),
    ],
)
def test_a_card_that_travelled_over_repeaters_parses_past_its_path(path_length_byte: int, path_size: int) -> None:
    contact_card_signer = ContactCardSigner()
    card_bytes = contact_card_signer.build_card_bytes(
        contact_card_signer.build_app_data(name="Far away"),
        path_length_byte=path_length_byte,
        path=bytes(range(path_size)),
    )

    assert parse_contact_card_uri("meshcore://" + card_bytes.hex()).name == "Far away"


def test_the_reserved_path_hash_size_is_refused() -> None:
    contact_card_signer = ContactCardSigner()
    card_bytes = contact_card_signer.build_card_bytes(contact_card_signer.build_app_data(), path_length_byte=0xC1)

    with pytest.raises(InvalidContactCardError, match="path length"):
        parse_contact_card_uri("meshcore://" + card_bytes.hex())


def test_app_data_over_32_bytes_is_clamped_as_the_firmware_does_before_verifying() -> None:
    contact_card_signer = ContactCardSigner()
    long_app_data = contact_card_signer.build_app_data(name="A name that is much longer than it may be")
    card_bytes = contact_card_signer.build_card_bytes(long_app_data, signed_app_data=long_app_data[:32])

    contact_card = parse_contact_card_uri("meshcore://" + card_bytes.hex())

    assert contact_card.name == "A name that is much longer than"
    assert len(contact_card.name.encode()) == 31


def test_a_signature_over_more_than_32_bytes_of_app_data_does_not_verify() -> None:
    contact_card_signer = ContactCardSigner()
    long_app_data = contact_card_signer.build_app_data(name="A name that is much longer than it may be")
    card_bytes = contact_card_signer.build_card_bytes(long_app_data)

    with pytest.raises(InvalidContactCardError, match="signature does not verify"):
        parse_contact_card_uri("meshcore://" + card_bytes.hex())


def test_a_card_without_a_name_has_an_empty_name() -> None:
    contact_card_signer = ContactCardSigner()
    card_bytes = contact_card_signer.build_card_bytes(contact_card_signer.build_app_data(name="", flags=0x01))

    assert parse_contact_card_uri("meshcore://" + card_bytes.hex()).name == ""


def test_the_location_and_feature_words_come_before_the_name() -> None:
    contact_card_signer = ContactCardSigner()
    location = struct.pack("<ii", -37_813_600, 144_963_100)
    app_data = bytes([0x01 | 0x10 | 0x20 | 0x80]) + location + b"\x00\x00" + b"Melbourne"
    card_bytes = contact_card_signer.build_card_bytes(app_data)

    contact_card = parse_contact_card_uri("meshcore://" + card_bytes.hex())

    assert contact_card.latitude_microdegrees == -37_813_600
    assert contact_card.longitude_microdegrees == 144_963_100
    assert contact_card.name == "Melbourne"


def test_a_repeater_card_parses_and_names_its_type_clearly() -> None:
    contact_card_signer = ContactCardSigner()
    contact_card = parse_contact_card_uri(contact_card_signer.build_card_uri(name="Hilltop", flags=0x82))

    assert contact_card.node_type == MeshCoreNodeType.REPEATER
    assert describe_node_type(contact_card.node_type) == "repeater"
    assert describe_node_type(9) == "node of unknown type 9"


@pytest.mark.parametrize(
    ("pasted_text", "expected_message"),
    [
        pytest.param("https://example.org/card", "starts with meshcore://", id="another scheme"),
        pytest.param("meshcore://11zz", "hexadecimal", id="not hex"),
        pytest.param("meshcore://", "empty", id="empty"),
        pytest.param("meshcore://1100" + "ab" * 40, "too short", id="truncated"),
        pytest.param("meshcore://10", "ends before its path length", id="only a header"),
    ],
)
def test_text_that_is_not_a_card_gets_a_message_saying_why(pasted_text: str, expected_message: str) -> None:
    with pytest.raises(InvalidContactCardError, match=expected_message):
        parse_contact_card_uri(pasted_text)


def test_a_key_that_is_no_curve_point_is_refused_like_a_bad_signature() -> None:
    card_bytes = bytearray.fromhex(SAMPLE_CONTACT_CARD_URI.removeprefix("meshcore://"))
    card_bytes[2:34] = b"\xff" * 32

    with pytest.raises(InvalidContactCardError, match="signature does not verify"):
        parse_contact_card_uri("meshcore://" + card_bytes.hex())
