"""Comparing what the node reports with the configuration in node_setting.

Two settings protect the contact table and are corrected at every handshake: with manual adding
off, or with any auto-add bit set, the node would add strangers itself or overwrite a user's
device when its table is full. Every other difference is only reported, for the operator's
"Re-apply configured settings".
"""

from dataclasses import dataclass
from typing import Any

from node.node_settings import NodeConfiguration, NodeSettingKey
from worker.node_gateway import AutoAddConfiguration, DeviceInformation, SelfInformation

# Radio values are kept as 32-bit floats on the node, so reading them back can be a unit off.
RADIO_READ_BACK_TOLERANCE = 1

CONTACT_SAFETY_KEYS = frozenset(
    {
        NodeSettingKey.CONTACTS_MANUAL_ADD,
        NodeSettingKey.CONTACTS_AUTO_ADD_CONFIGURATION,
        NodeSettingKey.CONTACTS_AUTO_ADD_MAXIMUM_HOPS,
    }
)
# The frame that corrects manual adding also writes these, from node_setting.
OTHER_PARAMETERS_KEYS = frozenset(
    {
        NodeSettingKey.MESSAGING_MULTI_ACKS,
        NodeSettingKey.PRIVACY_TELEMETRY_MODES,
        NodeSettingKey.PRIVACY_ADVERT_LOCATION_POLICY,
    }
)


@dataclass(frozen=True, kw_only=True)
class SettingDrift:
    key: NodeSettingKey
    expected: Any
    actual: Any
    corrected: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key.value, "expected": self.expected, "actual": self.actual, "corrected": self.corrected}


@dataclass(frozen=True, kw_only=True)
class ExpectedNodeSettings:
    """The node_setting values the node itself holds and reports."""

    node_name: str
    radio_frequency_kilohertz: int
    radio_bandwidth_hertz: int
    radio_spreading_factor: int
    radio_coding_rate: int
    radio_transmit_power_dbm: int
    radio_client_repeat: bool
    routing_path_hash_size: int
    messaging_multi_acks: int
    contacts_manual_add: bool
    contacts_auto_add_configuration: int
    contacts_auto_add_maximum_hops: int
    privacy_advert_location_policy: int
    privacy_telemetry_modes: int

    @classmethod
    def from_node_configuration(cls, node_configuration: NodeConfiguration) -> ExpectedNodeSettings:
        return cls(
            node_name=node_configuration.node_name,
            radio_frequency_kilohertz=node_configuration.radio_frequency_kilohertz,
            radio_bandwidth_hertz=node_configuration.radio_bandwidth_hertz,
            radio_spreading_factor=node_configuration.radio_spreading_factor,
            radio_coding_rate=node_configuration.radio_coding_rate,
            radio_transmit_power_dbm=node_configuration.radio_transmit_power_dbm,
            radio_client_repeat=node_configuration.radio_client_repeat,
            routing_path_hash_size=node_configuration.routing_path_hash_size,
            messaging_multi_acks=node_configuration.messaging_multi_acks,
            contacts_manual_add=node_configuration.contacts_manual_add,
            contacts_auto_add_configuration=node_configuration.contacts_auto_add_configuration,
            contacts_auto_add_maximum_hops=node_configuration.contacts_auto_add_maximum_hops,
            privacy_advert_location_policy=node_configuration.privacy_advert_location_policy,
            privacy_telemetry_modes=node_configuration.privacy_telemetry_modes,
        )


@dataclass(frozen=True, kw_only=True)
class ReportedNodeSettings:
    self_information: SelfInformation
    device_information: DeviceInformation
    auto_add_configuration: AutoAddConfiguration


def find_settings_drift(
    configuration: ExpectedNodeSettings, reported_settings: ReportedNodeSettings
) -> list[SettingDrift]:
    self_information = reported_settings.self_information
    expected_and_actual_values: list[tuple[NodeSettingKey, Any, Any]] = [
        (NodeSettingKey.NODE_NAME, configuration.node_name, self_information.name),
        (
            NodeSettingKey.RADIO_FREQUENCY_KILOHERTZ,
            configuration.radio_frequency_kilohertz,
            self_information.radio_frequency_kilohertz,
        ),
        (
            NodeSettingKey.RADIO_BANDWIDTH_HERTZ,
            configuration.radio_bandwidth_hertz,
            self_information.radio_bandwidth_hertz,
        ),
        (
            NodeSettingKey.RADIO_SPREADING_FACTOR,
            configuration.radio_spreading_factor,
            self_information.radio_spreading_factor,
        ),
        (NodeSettingKey.RADIO_CODING_RATE, configuration.radio_coding_rate, self_information.radio_coding_rate),
        (
            NodeSettingKey.RADIO_TRANSMIT_POWER_DBM,
            configuration.radio_transmit_power_dbm,
            self_information.transmit_power_dbm,
        ),
        (
            NodeSettingKey.RADIO_CLIENT_REPEAT,
            configuration.radio_client_repeat,
            reported_settings.device_information.client_repeat,
        ),
        (
            NodeSettingKey.ROUTING_PATH_HASH_SIZE,
            configuration.routing_path_hash_size,
            reported_settings.device_information.path_hash_size,
        ),
        (NodeSettingKey.MESSAGING_MULTI_ACKS, configuration.messaging_multi_acks, self_information.multi_acks),
        (NodeSettingKey.CONTACTS_MANUAL_ADD, configuration.contacts_manual_add, self_information.manual_add_contacts),
        (
            NodeSettingKey.CONTACTS_AUTO_ADD_CONFIGURATION,
            configuration.contacts_auto_add_configuration,
            reported_settings.auto_add_configuration.configuration,
        ),
        (
            NodeSettingKey.CONTACTS_AUTO_ADD_MAXIMUM_HOPS,
            configuration.contacts_auto_add_maximum_hops,
            reported_settings.auto_add_configuration.maximum_hops,
        ),
        (
            NodeSettingKey.PRIVACY_ADVERT_LOCATION_POLICY,
            configuration.privacy_advert_location_policy,
            self_information.advert_location_policy,
        ),
        (
            NodeSettingKey.PRIVACY_TELEMETRY_MODES,
            configuration.privacy_telemetry_modes,
            self_information.telemetry_modes,
        ),
    ]
    return [
        SettingDrift(key=key, expected=expected_value, actual=actual_value)
        for key, expected_value, actual_value in expected_and_actual_values
        if not values_match(key, expected_value, actual_value)
    ]


def values_match(key: NodeSettingKey, expected_value: Any, actual_value: Any) -> bool:
    if key in (NodeSettingKey.RADIO_FREQUENCY_KILOHERTZ, NodeSettingKey.RADIO_BANDWIDTH_HERTZ):
        return bool(abs(int(expected_value) - int(actual_value)) <= RADIO_READ_BACK_TOLERANCE)
    return bool(expected_value == actual_value)


def needs_contact_safety_correction(settings_drift: list[SettingDrift]) -> bool:
    return any(drift.key in CONTACT_SAFETY_KEYS for drift in settings_drift)


def mark_contact_safety_drift_corrected(settings_drift: list[SettingDrift]) -> list[SettingDrift]:
    corrected_keys = CONTACT_SAFETY_KEYS | OTHER_PARAMETERS_KEYS
    return [
        SettingDrift(key=drift.key, expected=drift.expected, actual=drift.actual, corrected=drift.key in corrected_keys)
        for drift in settings_drift
    ]
