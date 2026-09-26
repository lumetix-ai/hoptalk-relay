"""Session, clock and settings commands are answered and validated the way firmware v1.17.1 does."""

from typing import Any

from Crypto.Signature import eddsa
from meshcore import EventType
from meshcore.meshcore_parser import MeshcorePacketParser

from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.frames import PUBLIC_CHANNEL_SECRET, FirmwareErrorCode
from tests.worker.fake_node.meshcore_events import is_error_with_code
from tests.worker.fake_node.node_identity import parse_contact_card_uri

OK_OR_ERROR = [EventType.OK, EventType.ERROR]
VOLATILE_CLOCK_BOOT_TIME = 1715770351


async def send_raw_command(meshcore_client: Any, frame: bytes, expected_events: list[Any] | None = None) -> Any:
    return await meshcore_client.commands.send(frame, expected_events or OK_OR_ERROR)


async def test_device_info_describes_the_xiao_usb_build(meshcore_client: Any) -> None:
    device_information = await meshcore_client.commands.send_device_query()

    assert device_information.payload == {
        "fw ver": 13,
        "max_contacts": 350,
        "max_channels": 40,
        "ble_pin": 0,
        "fw_build": "14 Aug 2026",
        "model": "Seeed Xiao-nrf52",
        "ver": "v1.17.1-d929643",
        "repeat": False,
        "path_hash_mode": 0,
    }


async def test_self_info_reports_the_identity_and_the_default_settings(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    self_information = await meshcore_client.commands.send_appstart()

    assert self_information.payload["public_key"] == fake_companion_firmware.public_key.hex()
    assert self_information.payload["name"] == "hoptalk-relay"
    assert self_information.payload["radio_freq"] == 869.618
    assert self_information.payload["radio_bw"] == 62.5
    assert (self_information.payload["radio_sf"], self_information.payload["radio_cr"]) == (8, 5)
    assert (self_information.payload["tx_power"], self_information.payload["max_tx_power"]) == (22, 22)
    assert self_information.payload["manual_add_contacts"] is False


async def test_an_unknown_command_is_answered_unsupported(meshcore_client: Any) -> None:
    result = await send_raw_command(meshcore_client, b"\x77")

    assert is_error_with_code(result, FirmwareErrorCode.UNSUPPORTED_COMMAND)


async def test_a_command_shorter_than_its_minimum_is_answered_unsupported(meshcore_client: Any) -> None:
    short_text_message = b"\x02\x00\x00" + (1_800_000_000).to_bytes(4, "little") + b"\x11" * 6

    result = await send_raw_command(meshcore_client, short_text_message, [EventType.MSG_SENT, EventType.ERROR])

    assert is_error_with_code(result, FirmwareErrorCode.UNSUPPORTED_COMMAND)


async def test_the_clock_starts_at_the_boot_time_and_refuses_to_go_backwards(meshcore_client: Any) -> None:
    boot_time_result = await meshcore_client.commands.get_time()
    forward_result = await meshcore_client.commands.set_time(1_800_000_000)
    backward_result = await meshcore_client.commands.set_time(1_799_999_000)
    current_time_result = await meshcore_client.commands.get_time()

    assert abs(boot_time_result.payload["time"] - VOLATILE_CLOCK_BOOT_TIME) <= 1
    assert forward_result.type == EventType.OK
    assert is_error_with_code(backward_result, FirmwareErrorCode.ILLEGAL_ARGUMENT)
    assert current_time_result.payload["time"] in (1_800_000_000, 1_800_000_001)


async def test_the_node_name_is_cut_to_31_bytes_even_inside_a_character(meshcore_client: Any) -> None:
    await meshcore_client.commands.set_name("Реле" * 8)
    self_information = await meshcore_client.commands.send_appstart()

    # 32 two-byte letters make 64 bytes; the firmware keeps 31 bytes, which end with the first
    # byte of the 16th letter, and the library's decoder drops that byte.
    assert self_information.payload["name"] == "Реле" * 3 + "Рел"


async def test_radio_parameters_are_range_checked_and_the_repeat_byte_is_whitelisted(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    out_of_range_frequency = await meshcore_client.commands.set_radio(100.0, 62.5, 8, 5, 0)
    out_of_range_spreading_factor = await meshcore_client.commands.set_radio(869.525, 250.0, 13, 5, 0)
    repeat_on_other_frequency = await meshcore_client.commands.set_radio(869.525, 250.0, 11, 5, 1)
    repeat_on_whitelisted_frequency = await meshcore_client.commands.set_radio(869.495, 250.0, 11, 5, 1)
    omitted_repeat_byte = await meshcore_client.commands.set_radio(869.525, 250.0, 11, 5)
    self_information = await meshcore_client.commands.send_appstart()

    assert is_error_with_code(out_of_range_frequency, FirmwareErrorCode.ILLEGAL_ARGUMENT)
    assert is_error_with_code(out_of_range_spreading_factor, FirmwareErrorCode.ILLEGAL_ARGUMENT)
    assert is_error_with_code(repeat_on_other_frequency, FirmwareErrorCode.ILLEGAL_ARGUMENT)
    assert repeat_on_whitelisted_frequency.type == EventType.OK
    assert omitted_repeat_byte.type == EventType.OK
    assert fake_companion_firmware.preferences.client_repeat_enabled is False
    assert self_information.payload["radio_freq"] == 869.525
    assert self_information.payload["radio_bw"] == 250.0
    assert self_information.payload["radio_sf"] == 11


async def test_transmit_power_outside_minus_9_to_22_dbm_is_refused(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    too_low = await send_raw_command(meshcore_client, b"\x0c" + (-10).to_bytes(1, "little", signed=True))
    lowest = await send_raw_command(meshcore_client, b"\x0c" + (-9).to_bytes(1, "little", signed=True))
    too_high = await meshcore_client.commands.set_tx_power(23)

    assert is_error_with_code(too_low, FirmwareErrorCode.ILLEGAL_ARGUMENT)
    assert lowest.type == EventType.OK
    assert is_error_with_code(too_high, FirmwareErrorCode.ILLEGAL_ARGUMENT)
    assert fake_companion_firmware.preferences.transmit_power_dbm == -9


async def test_a_path_hash_mode_of_3_is_refused_and_a_nonzero_second_byte_is_unsupported(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    mode_three = await meshcore_client.commands.set_path_hash_mode(3)
    mode_two = await meshcore_client.commands.set_path_hash_mode(2)
    nonzero_second_byte = await send_raw_command(meshcore_client, b"\x3d\x01\x01")
    device_information = await meshcore_client.commands.send_device_query()

    assert is_error_with_code(mode_three, FirmwareErrorCode.ILLEGAL_ARGUMENT)
    assert mode_two.type == EventType.OK
    assert is_error_with_code(nonzero_second_byte, FirmwareErrorCode.UNSUPPORTED_COMMAND)
    assert device_information.payload["path_hash_mode"] == 2


async def test_other_parameters_are_read_byte_by_byte_as_far_as_the_frame_goes(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    await send_raw_command(meshcore_client, b"\x26\x01\x00\x00\x02")
    four_byte_frame = await send_raw_command(meshcore_client, b"\x26\x00\x15\x01")
    self_information = await meshcore_client.commands.send_appstart()

    assert four_byte_frame.type == EventType.OK
    assert self_information.payload["multi_acks"] == 2
    assert self_information.payload["manual_add_contacts"] is False
    assert self_information.payload["adv_loc_policy"] == 1
    assert (
        self_information.payload["telemetry_mode_base"],
        self_information.payload["telemetry_mode_loc"],
        self_information.payload["telemetry_mode_env"],
    ) == (1, 1, 1)


async def test_the_auto_add_hop_limit_is_optional_and_clamped_to_64(meshcore_client: Any) -> None:
    await send_raw_command(meshcore_client, b"\x3a\x02\x05")
    after_three_bytes = await meshcore_client.commands.get_autoadd_config()
    await send_raw_command(meshcore_client, b"\x3a\x00")
    after_two_bytes = await meshcore_client.commands.get_autoadd_config()
    await send_raw_command(meshcore_client, b"\x3a\x00\xc8")
    after_large_limit = await meshcore_client.commands.get_autoadd_config()

    assert after_three_bytes.payload == {"config": 2, "max_hops": 5}
    assert after_two_bytes.payload == {"config": 0, "max_hops": 5}
    assert after_large_limit.payload == {"config": 0, "max_hops": 64}


async def test_the_public_channel_can_be_read_and_replaced(meshcore_client: Any) -> None:
    public_channel = await meshcore_client.commands.get_channel(0)
    replaced = await meshcore_client.commands.set_channel(0, "Private", bytes(range(16)))
    private_channel = await meshcore_client.commands.get_channel(0)
    beyond_last_slot = await meshcore_client.commands.set_channel(40, "Nowhere", bytes(16))
    long_secret = await send_raw_command(meshcore_client, b"\x20\x01" + b"Long".ljust(32, b"\x00") + bytes(32))

    assert (public_channel.payload["channel_name"], public_channel.payload["channel_secret"]) == (
        "Public",
        PUBLIC_CHANNEL_SECRET,
    )
    assert replaced.type == EventType.OK
    assert (private_channel.payload["channel_name"], private_channel.payload["channel_secret"]) == (
        "Private",
        bytes(range(16)),
    )
    assert is_error_with_code(beyond_last_slot, FirmwareErrorCode.NOT_FOUND)
    assert is_error_with_code(long_secret, FirmwareErrorCode.UNSUPPORTED_COMMAND)


async def test_the_exported_card_is_a_signed_advert_the_library_parses(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    await meshcore_client.commands.set_time(1_800_000_000)

    export_result = await meshcore_client.commands.export_contact()
    card_uri = export_result.payload["uri"]
    card = bytes.fromhex(card_uri.removeprefix("meshcore://"))
    parsed_by_library = await MeshcorePacketParser().parsePacketPayload(card, {})
    parsed_advert = parse_contact_card_uri(card_uri)

    assert card[:2] == b"\x11\x00"
    assert parsed_by_library["adv_key"] == fake_companion_firmware.public_key.hex()
    assert parsed_by_library["adv_name"] == "hoptalk-relay"
    assert parsed_by_library["adv_type"] == 1
    assert parsed_advert is not None
    assert parsed_advert.timestamp in (1_800_000_000, 1_800_000_001)
    verifier = eddsa.new(eddsa.import_public_key(parsed_advert.public_key), "rfc8032")
    verifier.verify(parsed_advert.signed_message, parsed_advert.signature)


async def test_frames_the_firmware_would_misread_are_refused_and_recorded(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    short_radio_frame = await send_raw_command(meshcore_client, b"\x0b" + (869525).to_bytes(4, "little"))

    assert is_error_with_code(short_radio_frame, FirmwareErrorCode.ILLEGAL_ARGUMENT)
    assert len(fake_companion_firmware.protocol_violations) == 1
