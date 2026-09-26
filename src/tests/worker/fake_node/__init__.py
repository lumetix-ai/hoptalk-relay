"""An in-process MeshCore companion node and a simulated mesh, for tests that run the real meshcore library.

- `FakeCompanionFirmware` (fake_companion_firmware.py): companion firmware v1.17.1 as the XIAO
  nRF52840 USB build behaves: command frames, replies and pushes byte for byte, the tables and
  their limits, reboot and factory reset, plus controls to drop or delay replies.
- `FakeNodeTransport` and `FakeNodeConnector` (fake_node_transport.py): the connection MeshCore
  drives, through a real never-opened SerialConnection deframer; the connector is the injectable
  factory that returns a connected `meshcore.MeshCore`, and can be called again to reconnect.
- `SimulatedMesh`, `SimulatedDevice` and `LinkPolicy` (simulated_mesh.py): user devices with nodes
  of their own, joined to the relay's node by seeded, per-device and per-direction link policies.
- `frames.py`, `contact_records.py`, `node_identity.py`, `radio_packets.py`: byte layouts, contact
  records, Ed25519 identities and signed cards, and radio packets.
- `MeshCoreEventRecorder`, `is_lost_reply`, `is_error_with_code`,
  `wait_until_earlier_node_frames_are_dispatched` (meshcore_events.py): record the events a meshcore
  client dispatches, recognise its made-up "no reply" and the node's errors, and wait for pushes
  still on the serial link.
- `wait_until` (waiting.py): poll a condition instead of sleeping a fixed time.

The fixtures in fixtures.py, registered for every test by tests/conftest.py, build these with
fast timings and clean them up.
"""

from tests.worker.fake_node.contact_records import ContactRecord
from tests.worker.fake_node.fake_companion_firmware import (
    FactoryResetBehaviour,
    FakeCompanionFirmware,
    MessageSentReplyOrder,
    ReceptionOutcome,
    SendTextMessageResult,
    TextMessageQueued,
    TextMessageRejected,
)
from tests.worker.fake_node.fake_node_transport import (
    ConnectRaises,
    ConnectReturnsNothing,
    ConnectSucceeds,
    FakeByteStreamTransport,
    FakeNodeConnector,
    FakeNodeTransport,
    FakeNodeUnavailableError,
    SerialLinkTiming,
)
from tests.worker.fake_node.firmware_state import FirmwareBuild, FirmwareCapacities, FirmwareTiming, PowerState
from tests.worker.fake_node.meshcore_events import (
    MeshCoreEventRecorder,
    is_error_with_code,
    is_lost_reply,
    wait_until_earlier_node_frames_are_dispatched,
)
from tests.worker.fake_node.simulated_mesh import (
    DeliveryOutcome,
    DeviceUnreachableError,
    FirmwareAcknowledgement,
    LinkPolicy,
    ReceivedDirectMessage,
    SimulatedDevice,
    SimulatedMesh,
    TrafficRecord,
)
from tests.worker.fake_node.waiting import wait_until

__all__ = [
    "ConnectRaises",
    "ConnectReturnsNothing",
    "ConnectSucceeds",
    "ContactRecord",
    "DeliveryOutcome",
    "DeviceUnreachableError",
    "FactoryResetBehaviour",
    "FakeByteStreamTransport",
    "FakeCompanionFirmware",
    "FakeNodeConnector",
    "FakeNodeTransport",
    "FakeNodeUnavailableError",
    "FirmwareAcknowledgement",
    "FirmwareBuild",
    "FirmwareCapacities",
    "FirmwareTiming",
    "LinkPolicy",
    "MeshCoreEventRecorder",
    "MessageSentReplyOrder",
    "PowerState",
    "ReceivedDirectMessage",
    "ReceptionOutcome",
    "SendTextMessageResult",
    "SerialLinkTiming",
    "SimulatedDevice",
    "SimulatedMesh",
    "TextMessageQueued",
    "TextMessageRejected",
    "TrafficRecord",
    "is_error_with_code",
    "is_lost_reply",
    "wait_until",
    "wait_until_earlier_node_frames_are_dispatched",
]
