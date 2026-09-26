"""Typed access to node_setting, the configured node's key-value table.

The table holds either none of the NodeSettingKey keys (the server needs its initial setup) or
every one of them. Only replace_node_configuration(), at the end of a setup run, and
update_node_setting(), for the keys that may change later, write them.

Beside them the table may hold the keys of OptionalNodeSettingKey, which a configured node may
lack; node.node_identity_backups owns the only one. Replacing the configuration keeps them.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from django.db import transaction

from node.models import NodeSetting


class NodeSettingKey(StrEnum):
    NODE_PUBLIC_KEY = "node.public_key"
    NODE_NAME = "node.name"
    NODE_FIRMWARE_VERSION = "node.firmware_version"
    NODE_FIRMWARE_BUILD = "node.firmware_build"
    NODE_MODEL = "node.model"
    NODE_PROTOCOL_VERSION = "node.protocol_version"
    NODE_MAXIMUM_CONTACTS = "node.maximum_contacts"
    NODE_CONTACT_CARD_URI = "node.contact_card_uri"
    RADIO_PRESET_TITLE = "radio.preset_title"
    RADIO_FREQUENCY_KILOHERTZ = "radio.frequency_kilohertz"
    RADIO_BANDWIDTH_HERTZ = "radio.bandwidth_hertz"
    RADIO_SPREADING_FACTOR = "radio.spreading_factor"
    RADIO_CODING_RATE = "radio.coding_rate"
    RADIO_TRANSMIT_POWER_DBM = "radio.transmit_power_dbm"
    RADIO_CLIENT_REPEAT = "radio.client_repeat"
    ROUTING_PATH_HASH_SIZE = "routing.path_hash_size"
    MESSAGING_MULTI_ACKS = "messaging.multi_acks"
    CONTACTS_MANUAL_ADD = "contacts.manual_add"
    CONTACTS_AUTO_ADD_CONFIGURATION = "contacts.auto_add_configuration"
    CONTACTS_AUTO_ADD_MAXIMUM_HOPS = "contacts.auto_add_maximum_hops"
    PRIVACY_ADVERT_LOCATION_POLICY = "privacy.advert_location_policy"
    PRIVACY_TELEMETRY_MODES = "privacy.telemetry_modes"
    CHANNELS_PUBLIC_CHANNEL_REPLACED = "channels.public_channel_replaced"
    SETUP_COMPLETED_AT = "setup.completed_at"
    SETUP_RUN_ID = "setup.run_id"


class OptionalNodeSettingKey(StrEnum):
    # The encrypted private key of node.public_key (node.node_identity_backups).
    NODE_IDENTITY_BACKUP = "node.identity_backup"


OPTIONAL_NODE_SETTING_KEY_VALUES = frozenset(key.value for key in OptionalNodeSettingKey)
REQUIRED_NODE_SETTING_KEY_VALUES = frozenset(key.value for key in NodeSettingKey)


# The node reports these again on every handshake; the card is regenerated on request. Every
# other key changes only through a new setup run.
KEYS_UPDATABLE_AFTER_SETUP = frozenset(
    {
        NodeSettingKey.NODE_FIRMWARE_VERSION,
        NodeSettingKey.NODE_FIRMWARE_BUILD,
        NodeSettingKey.NODE_MODEL,
        NodeSettingKey.NODE_PROTOCOL_VERSION,
        NodeSettingKey.NODE_CONTACT_CARD_URI,
    }
)

MANUAL_RADIO_PRESET_TITLE = "manual"

BOOLEAN_TRUE_TEXT = "1"
BOOLEAN_FALSE_TEXT = "0"


@dataclass(frozen=True, kw_only=True)
class NodeConfiguration:
    """Every required node_setting key as a typed value; one field per NodeSettingKey, in the same order."""

    node_public_key: str
    node_name: str
    node_firmware_version: str
    node_firmware_build: str
    node_model: str
    node_protocol_version: int
    node_maximum_contacts: int
    # "meshcore://" followed by the advert in hex, without spaces.
    node_contact_card_uri: str
    # A bundled preset's title, or MANUAL_RADIO_PRESET_TITLE.
    radio_preset_title: str
    radio_frequency_kilohertz: int
    radio_bandwidth_hertz: int
    radio_spreading_factor: int
    radio_coding_rate: int
    radio_transmit_power_dbm: int
    # Always sent explicitly: a radio frame without the byte silently disables repeat.
    radio_client_repeat: bool
    routing_path_hash_size: int
    messaging_multi_acks: int
    contacts_manual_add: bool
    # The firmware's autoadd_config byte: type bits and the overwrite-oldest bit (0x01).
    contacts_auto_add_configuration: int
    contacts_auto_add_maximum_hops: int
    privacy_advert_location_policy: int
    privacy_telemetry_modes: int
    channels_public_channel_replaced: bool
    setup_completed_at: datetime
    setup_run_id: int


class IncompleteNodeConfigurationError(Exception):
    """node_setting holds some required keys but not all of them; shown as a fatal error."""


def load_node_configuration() -> NodeConfiguration | None:
    """Return None without any required key (setup required); raise IncompleteNodeConfigurationError for some."""
    stored_values = dict(
        NodeSetting.objects.filter(key__in=REQUIRED_NODE_SETTING_KEY_VALUES).values_list("key", "value")
    )
    if not stored_values:
        return None

    missing_keys = [key.value for key in NodeSettingKey if key.value not in stored_values]
    if missing_keys:
        raise IncompleteNodeConfigurationError(
            f"node_setting is incomplete: {', '.join(missing_keys)} missing. Run the setup wizard again."
        )

    return parse_node_configuration(stored_values)


def is_node_configured() -> bool:
    return NodeSetting.objects.exclude(key__in=OPTIONAL_NODE_SETTING_KEY_VALUES).exists()


def read_node_setting_value(key: NodeSettingKey | OptionalNodeSettingKey) -> str:
    """One stored value, or "" when the key is missing; unlike load_node_configuration() it never raises."""
    return NodeSetting.objects.filter(key=key.value).values_list("value", flat=True).first() or ""


def replace_node_configuration(node_configuration: NodeConfiguration) -> None:
    """Delete every row but the optional ones and insert every key in one transaction.

    configure_node calls it once it read the values back; the optional keys are the setup
    run's to keep, replace or delete in the same transaction.
    """
    serialized_values = serialize_node_configuration(node_configuration)
    with transaction.atomic():
        NodeSetting.objects.exclude(key__in=OPTIONAL_NODE_SETTING_KEY_VALUES).delete()
        NodeSetting.objects.bulk_create(
            NodeSetting(key=key.value, value=value) for key, value in serialized_values.items()
        )


def update_node_setting(key: NodeSettingKey, value: str) -> None:
    """Change one key of KEYS_UPDATABLE_AFTER_SETUP; any other key raises ValueError."""
    if key not in KEYS_UPDATABLE_AFTER_SETUP:
        raise ValueError(f"{key.value} changes only through a new setup run.")

    updated_row_count = NodeSetting.objects.filter(key=key.value).update(value=value)
    if updated_row_count == 0:
        # Inserting the row would leave a table that is neither empty nor complete.
        raise ValueError(f"{key.value} cannot be updated before the node has been set up.")


def serialize_node_configuration(node_configuration: NodeConfiguration) -> dict[NodeSettingKey, str]:
    return {
        NodeSettingKey.NODE_PUBLIC_KEY: node_configuration.node_public_key,
        NodeSettingKey.NODE_NAME: node_configuration.node_name,
        NodeSettingKey.NODE_FIRMWARE_VERSION: node_configuration.node_firmware_version,
        NodeSettingKey.NODE_FIRMWARE_BUILD: node_configuration.node_firmware_build,
        NodeSettingKey.NODE_MODEL: node_configuration.node_model,
        NodeSettingKey.NODE_PROTOCOL_VERSION: str(node_configuration.node_protocol_version),
        NodeSettingKey.NODE_MAXIMUM_CONTACTS: str(node_configuration.node_maximum_contacts),
        NodeSettingKey.NODE_CONTACT_CARD_URI: node_configuration.node_contact_card_uri,
        NodeSettingKey.RADIO_PRESET_TITLE: node_configuration.radio_preset_title,
        NodeSettingKey.RADIO_FREQUENCY_KILOHERTZ: str(node_configuration.radio_frequency_kilohertz),
        NodeSettingKey.RADIO_BANDWIDTH_HERTZ: str(node_configuration.radio_bandwidth_hertz),
        NodeSettingKey.RADIO_SPREADING_FACTOR: str(node_configuration.radio_spreading_factor),
        NodeSettingKey.RADIO_CODING_RATE: str(node_configuration.radio_coding_rate),
        NodeSettingKey.RADIO_TRANSMIT_POWER_DBM: str(node_configuration.radio_transmit_power_dbm),
        NodeSettingKey.RADIO_CLIENT_REPEAT: serialize_boolean(node_configuration.radio_client_repeat),
        NodeSettingKey.ROUTING_PATH_HASH_SIZE: str(node_configuration.routing_path_hash_size),
        NodeSettingKey.MESSAGING_MULTI_ACKS: str(node_configuration.messaging_multi_acks),
        NodeSettingKey.CONTACTS_MANUAL_ADD: serialize_boolean(node_configuration.contacts_manual_add),
        NodeSettingKey.CONTACTS_AUTO_ADD_CONFIGURATION: str(node_configuration.contacts_auto_add_configuration),
        NodeSettingKey.CONTACTS_AUTO_ADD_MAXIMUM_HOPS: str(node_configuration.contacts_auto_add_maximum_hops),
        NodeSettingKey.PRIVACY_ADVERT_LOCATION_POLICY: str(node_configuration.privacy_advert_location_policy),
        NodeSettingKey.PRIVACY_TELEMETRY_MODES: str(node_configuration.privacy_telemetry_modes),
        NodeSettingKey.CHANNELS_PUBLIC_CHANNEL_REPLACED: serialize_boolean(
            node_configuration.channels_public_channel_replaced
        ),
        NodeSettingKey.SETUP_COMPLETED_AT: node_configuration.setup_completed_at.isoformat(),
        NodeSettingKey.SETUP_RUN_ID: str(node_configuration.setup_run_id),
    }


def parse_node_configuration(stored_values: Mapping[str, str]) -> NodeConfiguration:
    reader = StoredValueReader(stored_values)
    return NodeConfiguration(
        node_public_key=reader.read_text(NodeSettingKey.NODE_PUBLIC_KEY),
        node_name=reader.read_text(NodeSettingKey.NODE_NAME),
        node_firmware_version=reader.read_text(NodeSettingKey.NODE_FIRMWARE_VERSION),
        node_firmware_build=reader.read_text(NodeSettingKey.NODE_FIRMWARE_BUILD),
        node_model=reader.read_text(NodeSettingKey.NODE_MODEL),
        node_protocol_version=reader.read_integer(NodeSettingKey.NODE_PROTOCOL_VERSION),
        node_maximum_contacts=reader.read_integer(NodeSettingKey.NODE_MAXIMUM_CONTACTS),
        node_contact_card_uri=reader.read_text(NodeSettingKey.NODE_CONTACT_CARD_URI),
        radio_preset_title=reader.read_text(NodeSettingKey.RADIO_PRESET_TITLE),
        radio_frequency_kilohertz=reader.read_integer(NodeSettingKey.RADIO_FREQUENCY_KILOHERTZ),
        radio_bandwidth_hertz=reader.read_integer(NodeSettingKey.RADIO_BANDWIDTH_HERTZ),
        radio_spreading_factor=reader.read_integer(NodeSettingKey.RADIO_SPREADING_FACTOR),
        radio_coding_rate=reader.read_integer(NodeSettingKey.RADIO_CODING_RATE),
        radio_transmit_power_dbm=reader.read_integer(NodeSettingKey.RADIO_TRANSMIT_POWER_DBM),
        radio_client_repeat=reader.read_boolean(NodeSettingKey.RADIO_CLIENT_REPEAT),
        routing_path_hash_size=reader.read_integer(NodeSettingKey.ROUTING_PATH_HASH_SIZE),
        messaging_multi_acks=reader.read_integer(NodeSettingKey.MESSAGING_MULTI_ACKS),
        contacts_manual_add=reader.read_boolean(NodeSettingKey.CONTACTS_MANUAL_ADD),
        contacts_auto_add_configuration=reader.read_integer(NodeSettingKey.CONTACTS_AUTO_ADD_CONFIGURATION),
        contacts_auto_add_maximum_hops=reader.read_integer(NodeSettingKey.CONTACTS_AUTO_ADD_MAXIMUM_HOPS),
        privacy_advert_location_policy=reader.read_integer(NodeSettingKey.PRIVACY_ADVERT_LOCATION_POLICY),
        privacy_telemetry_modes=reader.read_integer(NodeSettingKey.PRIVACY_TELEMETRY_MODES),
        channels_public_channel_replaced=reader.read_boolean(NodeSettingKey.CHANNELS_PUBLIC_CHANNEL_REPLACED),
        setup_completed_at=reader.read_datetime(NodeSettingKey.SETUP_COMPLETED_AT),
        setup_run_id=reader.read_integer(NodeSettingKey.SETUP_RUN_ID),
    )


def serialize_boolean(value: bool) -> str:
    return BOOLEAN_TRUE_TEXT if value else BOOLEAN_FALSE_TEXT


class StoredValueReader:
    """Converts stored texts to typed values; a value that does not convert makes the configuration unusable."""

    def __init__(self, stored_values: Mapping[str, str]) -> None:
        self.stored_values = stored_values

    def read_text(self, key: NodeSettingKey) -> str:
        return self.stored_values[key.value]

    def read_integer(self, key: NodeSettingKey) -> int:
        stored_value = self.stored_values[key.value]
        try:
            return int(stored_value)
        except ValueError as conversion_error:
            raise IncompleteNodeConfigurationError(
                f"node_setting {key.value} must be a whole number, not {stored_value!r}."
            ) from conversion_error

    def read_boolean(self, key: NodeSettingKey) -> bool:
        stored_value = self.stored_values[key.value]
        if stored_value not in (BOOLEAN_TRUE_TEXT, BOOLEAN_FALSE_TEXT):
            raise IncompleteNodeConfigurationError(f"node_setting {key.value} must be 0 or 1, not {stored_value!r}.")
        return stored_value == BOOLEAN_TRUE_TEXT

    def read_datetime(self, key: NodeSettingKey) -> datetime:
        stored_value = self.stored_values[key.value]
        try:
            return datetime.fromisoformat(stored_value)
        except ValueError as conversion_error:
            raise IncompleteNodeConfigurationError(
                f"node_setting {key.value} must be an ISO timestamp, not {stored_value!r}."
            ) from conversion_error
