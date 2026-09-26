"""What the read_node_information command reports about the attached node.

The worker builds a NodeInformation from its device query, self information, clock, auto-add
configuration, contact listing and channel 0, and stores to_json() as the command's result. The
setup run keeps the same JSON as original_node_information, and the wizard shows it before the
factory reset.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any


class InvalidNodeInformationError(ValueError):
    """The stored JSON lacks a field or holds a value of the wrong type."""


@dataclass(frozen=True, kw_only=True)
class NodeInformation:
    # Lower-case hex of the 32-byte key.
    public_key: str
    name: str
    firmware_version: str
    firmware_build: str
    model: str
    # The companion protocol version from the device query (13 for firmware v1.17.1).
    protocol_version: int
    maximum_contacts: int
    radio_frequency_kilohertz: int
    radio_bandwidth_hertz: int
    radio_spreading_factor: int
    radio_coding_rate: int
    client_repeat: bool
    transmit_power_dbm: int
    maximum_transmit_power_dbm: int
    # In bytes, 1 to 3: the firmware's path hash mode plus one.
    path_hash_size: int
    multi_acks: int
    manual_add_contacts: bool
    # The firmware's autoadd_config byte.
    auto_add_configuration: int
    auto_add_maximum_hops: int
    advert_location_policy: int
    telemetry_modes: int
    contact_count: int
    # The node's clock in Unix seconds and the server's time when it was read.
    node_clock_timestamp: int
    server_clock_timestamp: int
    channel_zero_name: str
    # True while channel 0 still has the well-known key of the public channel.
    channel_zero_is_public: bool

    @property
    def clock_offset_seconds(self) -> int:
        """The node's clock minus the server's."""
        return self.node_clock_timestamp - self.server_clock_timestamp

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, stored_json: Mapping[str, Any]) -> NodeInformation:
        field_values: dict[str, Any] = {}
        for field_name, field_type in NODE_INFORMATION_FIELD_TYPES.items():
            if field_name not in stored_json:
                raise InvalidNodeInformationError(f"The node information has no {field_name}.")
            field_value = stored_json[field_name]
            if not has_json_type(field_value, field_type):
                raise InvalidNodeInformationError(
                    f"The node information's {field_name} must be {field_type.__name__}, not {field_value!r}."
                )
            field_values[field_name] = field_value
        return cls(**field_values)


def has_json_type(field_value: object, field_type: type) -> bool:
    # bool is a subclass of int, and JSON keeps them apart.
    if field_type is int:
        return isinstance(field_value, int) and not isinstance(field_value, bool)
    return isinstance(field_value, field_type)


NODE_INFORMATION_FIELD_TYPES: Mapping[str, type] = {
    "public_key": str,
    "name": str,
    "firmware_version": str,
    "firmware_build": str,
    "model": str,
    "protocol_version": int,
    "maximum_contacts": int,
    "radio_frequency_kilohertz": int,
    "radio_bandwidth_hertz": int,
    "radio_spreading_factor": int,
    "radio_coding_rate": int,
    "client_repeat": bool,
    "transmit_power_dbm": int,
    "maximum_transmit_power_dbm": int,
    "path_hash_size": int,
    "multi_acks": int,
    "manual_add_contacts": bool,
    "auto_add_configuration": int,
    "auto_add_maximum_hops": int,
    "advert_location_policy": int,
    "telemetry_modes": int,
    "contact_count": int,
    "node_clock_timestamp": int,
    "server_clock_timestamp": int,
    "channel_zero_name": str,
    "channel_zero_is_public": bool,
}
