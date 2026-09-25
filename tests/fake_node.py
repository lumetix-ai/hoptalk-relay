"""A stand-in for a USB-attached MeshCore node, for tests without hardware.

FakeNode implements the same interface `SerialConnection` offers MeshCore
(connect/disconnect/send/set_reader/set_disconnect_callback) and answers the
companion-protocol commands these scripts use, so the real MeshCore reader,
dispatcher and command handlers run unchanged.

Replies are delivered from a background task after a short delay, the way a real
serial link does. That matters: the library's own get_contacts() registers its
waiters only after sending, so an instant in-process reply would make it time out
in a way real hardware never does.
"""

from __future__ import annotations

import asyncio
from typing import Optional

# Frame codes, named the way the firmware does (companion_radio/MyMesh.cpp).
RESP_OK = 0
RESP_ERR = 1
RESP_CONTACTS_START = 2
RESP_CONTACT = 3
RESP_END_OF_CONTACTS = 4
RESP_SELF_INFO = 5
RESP_SENT = 6
RESP_CONTACT_MSG_RECV = 7
RESP_CHANNEL_MSG_RECV = 8
RESP_NO_MORE_MESSAGES = 10
RESP_EXPORT_CONTACT = 11
RESP_DEVICE_INFO = 13
RESP_CONTACT_MSG_RECV_V3 = 16
RESP_CHANNEL_INFO = 18
RESP_AUTOADD_CONFIG = 25
PUSH_ADVERT = 0x80
PUSH_ACK = 0x82
PUSH_MSG_WAITING = 0x83
PUSH_NEW_ADVERT = 0x8A
PUSH_PATH_DISCOVERY = 0x8D

ERR_NOT_FOUND = 2
ERR_ILLEGAL_ARG = 6

# Contact keys the tests share.
ALICE_KEY = bytes.fromhex("11223344556677") + b"\x00" * 25
BOB_KEY = bytes.fromhex("aabbccddeeff") + b"\x11" * 26
PAGER_KEY = bytes.fromhex("99887766554433") + b"\x22" * 25
STRANGER_KEY = bytes.fromhex("deadbeefcafe") + b"\x33" * 26
CARD_KEY = bytes.fromhex("0f0e0d0c0b0a09") + b"\x44" * 25

ACK_CODE = bytes.fromhex("deadbeef")


# ── frame builders ───────────────────────────────────────────────────────
def contact_frame(
    pubkey: bytes, name: str, code: int = RESP_CONTACT, path: Optional[bytes] = None
) -> bytearray:
    """A contact record. path=None means no stored route, b"" means zero hops."""
    plen = 255 if path is None else len(path)
    f = bytearray([code])
    f += pubkey + bytes([1, 0, plen]) + (path or b"").ljust(64, b"\0")
    f += name.encode().ljust(32, b"\0")[:32]
    f += (0).to_bytes(4, "little") + (0).to_bytes(4, "little", signed=True) * 2
    f += (1).to_bytes(4, "little")
    return f


def dm_frame(
    prefix: bytes,
    text: str,
    ts: int,
    path_len: int = 255,
    txt_type: int = 0,
    signature: bytes = b"\x01\x02\x03\x04",
):
    f = bytearray([RESP_CONTACT_MSG_RECV]) + prefix + bytes([path_len, txt_type])
    f += ts.to_bytes(4, "little")
    if txt_type == 2:
        # A signed message carries four signature bytes before the text.
        f += signature
    return f + text.encode()


def dm_v3_frame(prefix: bytes, text: str, ts: int, snr_quarters: int = -30, path_len: int = 2):
    f = bytearray([RESP_CONTACT_MSG_RECV_V3])
    f += snr_quarters.to_bytes(1, "little", signed=True) + bytes(2)
    f += prefix + bytes([path_len, 0]) + ts.to_bytes(4, "little") + text.encode()
    return f


def channel_msg_frame(idx: int, text: str, ts: int, path_len: int = 255):
    f = bytearray([RESP_CHANNEL_MSG_RECV]) + bytes([idx, path_len, 0])
    return f + ts.to_bytes(4, "little") + text.encode()


def channel_info_frame(idx: int, name: str, secret: bytes) -> bytearray:
    return (
        bytearray([RESP_CHANNEL_INFO])
        + bytes([idx])
        + name.encode().ljust(32, b"\0")[:32]
        + secret
    )


def path_discovery_frame(
    pubkey: bytes, out_hops: bytes, in_hops: bytes, hash_mode: int = 0
) -> bytearray:
    """The answer to a path discovery: the route out, then the route back."""
    hop = hash_mode + 1
    f = bytearray([PUSH_PATH_DISCOVERY, 0]) + pubkey[:6]
    f += bytes([(len(out_hops) // hop) | (hash_mode << 6)]) + out_hops
    f += bytes([(len(in_hops) // hop) | (hash_mode << 6)]) + in_hops
    return f


def advert_push(pubkey: bytes) -> bytearray:
    return bytearray([PUSH_ADVERT]) + pubkey


def pending_contact_push(pubkey: bytes, name: str) -> bytearray:
    return contact_frame(pubkey, name, code=PUSH_NEW_ADVERT)


# ── the node ─────────────────────────────────────────────────────────────
class FakeNode:
    """One fake node, configurable per test."""

    def __init__(
        self,
        *,
        name: str = "hoptalk-relay",
        freq: float = 916.575,
        bw: float = 62.5,
        sf: int = 7,
        cr: int = 7,
        manual_add: int = 0,
        autoadd_config: int = 0x00,
        autoadd_max_hops: int = 0,
        autoadd_supported: bool = True,
        multi_acks: int = 0,
        path_hash_mode: int = 0,
        protocol: int = 13,
        firmware: str = "v1.17.1-d929643",
        answers_device_query: bool = True,
        contacts: Optional[list[tuple[bytes, str]]] = None,
        channels: Optional[dict[int, tuple[str, bytes]]] = None,
        send_msg_mode: str = "ack",
        reset_mode: str = "ok",
        discovery_mode: str = "ok",
        discovery_out: bytes = b"\x11\x22",
        discovery_in: bytes = b"\x33",
        reply_delay: float = 0.01,
    ) -> None:
        self.name = name
        self.freq, self.bw, self.sf, self.cr = freq, bw, sf, cr
        self.manual_add = manual_add
        self.autoadd_config = autoadd_config
        self.autoadd_max_hops = autoadd_max_hops
        self.autoadd_supported = autoadd_supported
        self.multi_acks = multi_acks
        self.path_hash_mode = path_hash_mode
        self.protocol = protocol
        self.firmware = firmware
        self.answers_device_query = answers_device_query
        # (key, name) or (key, name, stored path); path None means flood-only.
        self.contacts: list[tuple] = [
            (entry + (None,))[:3] if len(entry) < 3 else entry for entry in (contacts or [])
        ]
        self.channels = dict(channels or {})
        self.send_msg_mode = send_msg_mode
        self.reset_mode = reset_mode
        self.discovery_mode = discovery_mode
        self.discovery_out = discovery_out
        self.discovery_in = discovery_in
        self.reply_delay = reply_delay

        self.pending: list[bytearray] = []      # messages queued on the node
        self.sent_commands: list[bytes] = []    # every frame the script sent
        self.sent_texts: list[str] = []         # text of every outgoing message
        self.reader = None
        self._tasks: set[asyncio.Task] = set()

    # -- SerialConnection interface -------------------------------------
    def set_reader(self, reader) -> None:
        self.reader = reader

    def set_disconnect_callback(self, callback) -> None:
        self._disconnect_callback = callback

    async def connect(self) -> str:
        return "fake"

    async def disconnect(self) -> None:
        pass

    # -- plumbing -------------------------------------------------------
    async def push(self, frame) -> None:
        """Deliver a frame to the script, as an unsolicited push would arrive."""
        await self.reader.handle_rx(bytearray(frame))

    async def reply(self, frames: list, delay: Optional[float] = None) -> None:
        async def _later() -> None:
            await asyncio.sleep(self.reply_delay if delay is None else delay)
            for frame in frames:
                await self.push(frame)

        task = asyncio.create_task(_later())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def commands(self, code: int) -> list[bytes]:
        return [c for c in self.sent_commands if c[0] == code]

    # -- frames describing this node ------------------------------------
    def self_info(self) -> bytearray:
        f = bytearray([RESP_SELF_INFO])
        f += bytes([1, 22, 22]) + b"\xcd" * 32
        f += (0).to_bytes(4, "little", signed=True) * 2
        f += bytes([self.multi_acks, 0, 0, self.manual_add])
        f += int(self.freq * 1000).to_bytes(4, "little")
        f += int(self.bw * 1000).to_bytes(4, "little")
        f += bytes([self.sf, self.cr])
        f += self.name.encode()
        return f

    def device_info(self) -> bytearray:
        f = bytearray([RESP_DEVICE_INFO, self.protocol])
        if self.protocol >= 3:
            f += bytes([50, 8]) + (0).to_bytes(4, "little")
            f += b"14-Aug-2026".ljust(12, b"\0")
            f += b"Seeed Xiao-nrf52".ljust(40, b"\0")
            f += self.firmware.encode().ljust(20, b"\0")
        if self.protocol >= 9:
            f += bytes([0])
        if self.protocol >= 10:
            f += bytes([self.path_hash_mode])
        return f

    def contact_path(self, key_or_prefix: bytes) -> Optional[bytes]:
        for key, _name, path in self.contacts:
            if key.startswith(key_or_prefix) or key_or_prefix.startswith(key):
                return path
        return None

    def add_contact(self, key: bytes, name: str, path: Optional[bytes] = None) -> None:
        self.contacts = [c for c in self.contacts if c[0] != key] + [(key, name, path)]

    def set_contact_path(self, key: bytes, path: Optional[bytes]) -> None:
        self.contacts = [
            (k, n, path if k == key else p) for k, n, p in self.contacts
        ]

    def contact_frames(self) -> list[bytearray]:
        frames = [bytearray([RESP_CONTACTS_START]) + len(self.contacts).to_bytes(4, "little")]
        frames += [contact_frame(key, name, path=path) for key, name, path in self.contacts]
        frames.append(bytearray([RESP_END_OF_CONTACTS]) + (1).to_bytes(4, "little"))
        return frames

    # -- command handling ------------------------------------------------
    async def send(self, data: bytes) -> None:
        data = bytes(data)
        self.sent_commands.append(data)
        cmd = data[0]

        if cmd == 0x01:                                  # APP_START
            await self.reply([self.self_info()])
        elif cmd == 0x02:                                # SEND_TXT_MSG
            self.sent_texts.append(data[13:].decode())
            if self.send_msg_mode == "error":
                await self.reply([bytearray([RESP_ERR, ERR_NOT_FOUND])])
            else:
                # The firmware routes along a stored path when it has one, and
                # floods otherwise; type 1 in MSG_SENT means it flooded.
                routed_flood = self.contact_path(data[7:13]) is None
                frames = [
                    bytearray([RESP_SENT, 1 if routed_flood else 0])
                    + ACK_CODE
                    + (500).to_bytes(4, "little")
                ]
                if self.send_msg_mode == "ack":
                    frames.append(bytearray([PUSH_ACK]) + ACK_CODE)
                await self.reply(frames)
        elif cmd == 0x04:                                # GET_CONTACTS
            await self.reply(self.contact_frames())
        elif cmd == 0x08:                                # SET_ADVERT_NAME
            self.name = data[1:].decode()
            await self.reply([bytearray([RESP_OK])])
        elif cmd == 0x09:                                # ADD_UPDATE_CONTACT
            key = data[1:33]
            name = data[100:132].rstrip(b"\0").decode("utf-8", "ignore")
            self.add_contact(key, name)
            await self.reply([bytearray([RESP_OK])])
        elif cmd == 0x0A:                                # SYNC_NEXT_MESSAGE
            if self.pending:
                await self.reply([self.pending.pop(0)])
            else:
                await self.reply([bytearray([RESP_NO_MORE_MESSAGES])])
        elif cmd == 0x34:                                # SEND_PATH_DISCOVERY_REQ
            if self.discovery_mode == "error":
                await self.reply([bytearray([RESP_ERR, ERR_NOT_FOUND])])
                return
            await self.reply([bytearray([RESP_SENT, 1]) + ACK_CODE + (4400).to_bytes(4, "little")])
            if self.discovery_mode == "ok":
                # A real answer crosses the mesh and comes back seconds later.
                await self.reply(
                    [path_discovery_frame(data[2:34], self.discovery_out, self.discovery_in)],
                    delay=0.3,
                )
        elif cmd == 0x0D:                                # RESET_PATH
            self.set_contact_path(data[1:33], None)
            await self.reply([bytearray([RESP_OK])])
        elif cmd == 0x0B:                                # SET_RADIO_PARAMS
            self.freq = int.from_bytes(data[1:5], "little") / 1000
            self.bw = int.from_bytes(data[5:9], "little") / 1000
            self.sf, self.cr = data[9], data[10]
            await self.reply([bytearray([RESP_OK])])
        elif cmd == 0x0F:                                # REMOVE_CONTACT
            key = data[1:33]
            self.contacts = [c for c in self.contacts if c[0] != key]
            await self.reply([bytearray([RESP_OK])])
        elif cmd == 0x11:                                # EXPORT_CONTACT (self)
            card = b"\x01" + b"\xcd" * 32 + self.name.encode()
            await self.reply([bytearray([RESP_EXPORT_CONTACT]) + card])
        elif cmd == 0x12:                                # IMPORT_CONTACT
            self.add_contact(CARD_KEY, "CardPager")
            await self.reply([bytearray([RESP_OK])])
        elif cmd == 0x13:                                # REBOOT (never answers)
            pass
        elif cmd == 0x16:                                # DEVICE_QUERY
            if self.answers_device_query:
                await self.reply([self.device_info()])
        elif cmd == 0x1F:                                # GET_CHANNEL
            idx = data[1]
            if idx not in self.channels:
                await self.reply([bytearray([RESP_ERR, ERR_NOT_FOUND])])
            else:
                name, secret = self.channels[idx]
                await self.reply([channel_info_frame(idx, name, secret)])
        elif cmd == 0x20:                                # SET_CHANNEL
            idx = data[1]
            name = data[2:34].rstrip(b"\0").decode()
            self.channels[idx] = (name, bytes(data[34:50]))
            await self.reply([bytearray([RESP_OK])])
        elif cmd == 0x26:                                # SET_OTHER_PARAMS
            self.manual_add = data[1]
            if len(data) >= 5:
                self.multi_acks = data[4]
            await self.reply([bytearray([RESP_OK])])
        elif cmd == 0x33:                                # FACTORY_RESET
            if self.reset_mode == "ok":
                await self.reply([bytearray([RESP_OK])])
            elif self.reset_mode == "error":
                await self.reply([bytearray([RESP_ERR, 1])])
            # "silent": the firmware disables serial before it can answer
        elif cmd == 0x3A:                                # SET_AUTOADD_CONFIG
            self.autoadd_config = data[1]
            await self.reply([bytearray([RESP_OK])])
        elif cmd == 0x3B:                                # GET_AUTOADD_CONFIG
            if self.autoadd_supported:
                await self.reply(
                    [bytearray([RESP_AUTOADD_CONFIG, self.autoadd_config, self.autoadd_max_hops])]
                )
            else:
                await self.reply([bytearray([RESP_ERR, 1])])
        elif cmd == 0x3D:                                # SET_PATH_HASH_MODE
            if data[2] >= 3:
                await self.reply([bytearray([RESP_ERR, ERR_ILLEGAL_ARG])])
            else:
                self.path_hash_mode = data[2]
                await self.reply([bytearray([RESP_OK])])
        else:
            await self.reply([bytearray([RESP_OK])])


class FakeTTY:
    """stdin stand-in that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


def scripted_input(answers: list[str]):
    """input() stand-in that returns prepared answers, then raises EOFError."""
    answers = iter(answers)

    def _input(prompt: str = "") -> str:
        return next(answers)

    return _input


def port_schedule(sequence: list[list[str]]):
    """candidate_ports() stand-in walking a schedule, repeating its last step."""
    calls = {"n": 0}

    def _ports() -> list[str]:
        index = min(calls["n"], len(sequence) - 1)
        calls["n"] += 1
        return sequence[index]

    return _ports


def any_line(captured: str, needle: str) -> bool:
    return any(needle in line for line in captured.splitlines())
