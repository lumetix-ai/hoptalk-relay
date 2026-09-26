"""Node information, configurations and signed contact cards for service and panel tests."""

from datetime import UTC, datetime

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from node.node_information import NodeInformation
from node.node_settings import NodeConfiguration
from tests.node_key_pairs import generate_node_key_pair

# meshcore.js examples/parse_advert.js: a real card exported by a companion node.
SAMPLE_CONTACT_CARD_URI = (
    "meshcore://1100e04b135959ffac9397b600add84822cb8bf4a050a7f40965dd1ab7aea3ddd3743327e668b5db95bc8fbc38"
    "94b115415d6e4cca36f9c9e62e923afd37c3e2a154b27b0c53b6cfddd45bb3faf56fdaf08860d985ca2da44f9dcac1d7d76f"
    "c2b86d7b26e004814c69616d20436f74746c6520f09fa4a0"
)
SAMPLE_CONTACT_CARD_PUBLIC_KEY = "e04b135959ffac9397b600add84822cb8bf4a050a7f40965dd1ab7aea3ddd374"
SAMPLE_CONTACT_CARD_TIMESTAMP = 1759913779
SAMPLE_CONTACT_CARD_NAME = "Liam Cottle 🤠"

# Real key pairs, so that an identity backup of either node can be stored.
ORIGINAL_NODE_KEY_PAIR = generate_node_key_pair(11)
RESET_NODE_KEY_PAIR = generate_node_key_pair(22)
ORIGINAL_NODE_PUBLIC_KEY = ORIGINAL_NODE_KEY_PAIR.public_key
RESET_NODE_PUBLIC_KEY = RESET_NODE_KEY_PAIR.public_key

FLOOD_ADVERT_HEADER = 0x11
CHAT_NODE_WITH_NAME_FLAGS = 0x81


def build_node_information(public_key: str = ORIGINAL_NODE_PUBLIC_KEY, name: str = "Old relay") -> NodeInformation:
    return NodeInformation(
        public_key=public_key,
        name=name,
        firmware_version="v1.17.1-d929643",
        firmware_build="14-Aug-2026",
        model="Seeed Xiao-nrf52",
        protocol_version=13,
        maximum_contacts=350,
        radio_frequency_kilohertz=910525,
        radio_bandwidth_hertz=62500,
        radio_spreading_factor=7,
        radio_coding_rate=5,
        client_repeat=False,
        transmit_power_dbm=20,
        maximum_transmit_power_dbm=22,
        path_hash_size=3,
        multi_acks=0,
        manual_add_contacts=False,
        auto_add_configuration=0x1E,
        auto_add_maximum_hops=0,
        advert_location_policy=0,
        telemetry_modes=0,
        contact_count=12,
        node_clock_timestamp=1_790_000_004,
        server_clock_timestamp=1_790_000_000,
        channel_zero_name="Public",
        channel_zero_is_public=True,
    )


def build_node_configuration(
    public_key: str = RESET_NODE_PUBLIC_KEY,
    setup_run_id: int = 1,
    contact_card_uri: str = SAMPLE_CONTACT_CARD_URI,
) -> NodeConfiguration:
    return NodeConfiguration(
        node_public_key=public_key,
        node_name="HopTalk Relay",
        node_firmware_version="v1.17.1-d929643",
        node_firmware_build="14-Aug-2026",
        node_model="Seeed Xiao-nrf52",
        node_protocol_version=13,
        node_maximum_contacts=350,
        node_contact_card_uri=contact_card_uri,
        radio_preset_title="Australia (Narrow)",
        radio_frequency_kilohertz=916575,
        radio_bandwidth_hertz=62500,
        radio_spreading_factor=7,
        radio_coding_rate=7,
        radio_transmit_power_dbm=22,
        radio_client_repeat=False,
        routing_path_hash_size=2,
        messaging_multi_acks=2,
        contacts_manual_add=True,
        contacts_auto_add_configuration=0,
        contacts_auto_add_maximum_hops=0,
        privacy_advert_location_policy=0,
        privacy_telemetry_modes=0,
        channels_public_channel_replaced=False,
        setup_completed_at=datetime(2026, 9, 25, 12, 30, tzinfo=UTC),
        setup_run_id=setup_run_id,
    )


class ContactCardSigner:
    """Builds signed cards the way the firmware exports them, with any layout variation a test needs."""

    def __init__(self) -> None:
        self.signing_key = ECC.generate(curve="ed25519")
        self.public_key_bytes: bytes = self.signing_key.public_key().export_key(format="raw")

    @property
    def public_key(self) -> str:
        return self.public_key_bytes.hex()

    def build_app_data(
        self, name: str = "Alice", flags: int = CHAT_NODE_WITH_NAME_FLAGS, location: bytes = b""
    ) -> bytes:
        return bytes([flags]) + location + name.encode()

    def build_card_bytes(
        self,
        app_data: bytes,
        header: int = FLOOD_ADVERT_HEADER,
        bytes_before_path_length: bytes = b"",
        path_length_byte: int = 0,
        path: bytes = b"",
        timestamp: int = 1_790_000_000,
        signed_app_data: bytes | None = None,
    ) -> bytes:
        timestamp_bytes = timestamp.to_bytes(4, "little")
        signed_part = signed_app_data if signed_app_data is not None else app_data
        signature = eddsa.new(self.signing_key, "rfc8032").sign(self.public_key_bytes + timestamp_bytes + signed_part)
        return (
            bytes([header])
            + bytes_before_path_length
            + bytes([path_length_byte])
            + path
            + self.public_key_bytes
            + timestamp_bytes
            + signature
            + app_data
        )

    def build_card_uri(self, name: str = "Alice", flags: int = CHAT_NODE_WITH_NAME_FLAGS) -> str:
        return "meshcore://" + self.build_card_bytes(self.build_app_data(name=name, flags=flags)).hex()
