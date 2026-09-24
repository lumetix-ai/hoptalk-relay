"""Shared plumbing for the scripts that talk to the USB-attached MeshCore node."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any, Optional

from meshcore import EventType, MeshCore
from serial.tools import list_ports

DEFAULT_BAUDRATE = 115200

# What these scripts were actually checked against: a Seeed XIAO nRF52840 running
# MeshCore companion firmware v1.17.1 (build 14-Aug-2026), which reports
# companion-protocol version 13. The protocol version is what matters — the
# firmware string is only there to name the build in messages.
TESTED_PROTOCOL = 13
TESTED_FIRMWARE = "v1.17.1"

# Protocol version each thing these scripts use first appeared in, so an older
# node can be told what it will be missing instead of just failing at it.
FEATURE_MIN_PROTOCOL = (
    (7, "direct message acks (multi_acks)"),
    (10, "default path hash size"),
)

# macOS exposes a USB CDC node (XIAO nRF52840) as /dev/cu.usbmodem*; the other
# hints cover the common USB-UART bridges, in case the board sits behind one.
PORT_HINTS = ("usbmodem", "usbserial", "wchusbserial", "SLAB_USBtoUART")


def status(msg: str) -> None:
    """Status line — stderr, so stdout stays a clean data stream."""
    print(msg, file=sys.stderr, flush=True)


def candidate_ports() -> list[str]:
    """Serial ports that plausibly belong to a MeshCore companion node."""
    return [
        p.device
        for p in list_ports.comports()
        if any(hint in p.device for hint in PORT_HINTS)
    ]


def describe_ports() -> str:
    lines = []
    for p in list_ports.comports():
        vid = f"{p.vid:04x}" if p.vid is not None else "----"
        pid = f"{p.pid:04x}" if p.pid is not None else "----"
        lines.append(f"  {p.device}  [{vid}:{pid}]  {p.description}")
    return "\n".join(lines) or "  (no serial ports found)"


def add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--port", help="serial port (default: auto-detect, or $MESHCORE_PORT)"
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=DEFAULT_BAUDRATE,
        help="baud rate (default: %(default)s)",
    )
    parser.add_argument(
        "--list-ports", action="store_true", help="list serial ports and exit"
    )
    parser.add_argument(
        "--debug", action="store_true", help="verbose meshcore protocol logging"
    )


def configure_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if not debug:
        logging.getLogger("meshcore").setLevel(logging.ERROR)


def resolve_port(args: argparse.Namespace) -> Optional[str]:
    if args.port:
        return args.port

    env_port = os.environ.get("MESHCORE_PORT")
    if env_port:
        return env_port

    found = candidate_ports()
    if not found:
        status("No USB serial port found. Ports currently visible:")
        status(describe_ports())
        status("Plug the node in, then retry (or pass --port explicitly).")
        return None
    if len(found) > 1:
        status(f"Several candidate ports found: {', '.join(found)}")
        status(f"Using {found[0]} — pass --port to pick another.")
    return found[0]


async def connect(
    args: argparse.Namespace, *, port: Optional[str] = None, **kwargs
) -> Optional[MeshCore]:
    """Open the node, or explain why it could not be opened."""
    port = port or resolve_port(args)
    if port is None:
        return None

    status(f"Connecting to {port} at {args.baud} baud ...")
    try:
        mc = await MeshCore.create_serial(
            port, baudrate=args.baud, debug=args.debug, **kwargs
        )
    except (ConnectionError, OSError) as exc:
        status(f"Could not open {port}: {exc}")
        return None

    if mc is None:
        status(
            "Port opened, but the node did not answer. Is the firmware built "
            "as a USB/serial companion, and is no other app (web client, "
            "mccli) holding the port?"
        )
        return None
    return mc


async def fetch_contacts(mc: MeshCore, timeout: float = 10.0) -> Optional[dict[str, Any]]:
    """Read the contact list, subscribing *before* the request goes out.

    The library's own get_contacts() registers its waiters after sending, so a
    node that answers immediately can have its whole reply land before anything
    is listening — which shows up as a spurious timeout. Returns the node's
    view of its contacts, or None if it never answered.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def on_contacts(event) -> None:
        if not fut.done():
            fut.set_result(event.payload)

    sub = mc.subscribe(EventType.CONTACTS, on_contacts)
    try:
        await mc.commands.get_contacts_async()
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        status("Timed out reading the contact list from the node.")
        return None
    finally:
        mc.unsubscribe(sub)


async def check_firmware(mc: MeshCore) -> Optional[dict[str, Any]]:
    """Report the node's firmware and warn when it is not what we verified against.

    Never blocks: this is about the byte-level details these scripts hardcode
    from the firmware source (the factory-reset payload, the auto-add bits),
    which a different firmware could have changed without any error surfacing.
    """
    res = await mc.commands.send_device_query()
    if res is None or res.type == EventType.ERROR:
        status(
            "! the node did not answer a device query, so its firmware version is "
            f"unknown; these scripts were checked against protocol {TESTED_PROTOCOL} "
            f"({TESTED_FIRMWARE})."
        )
        return None

    info = res.payload
    proto = info.get("fw ver")
    status(
        f"Firmware: {info.get('ver', '?')} ({info.get('model', '?')}, "
        f"build {info.get('fw_build', '?')}, companion protocol {proto})"
    )

    if proto is None:
        status(f"! no protocol version reported; expected {TESTED_PROTOCOL}.")
    elif proto < TESTED_PROTOCOL:
        status(
            f"! protocol {proto} is older than the {TESTED_PROTOCOL} these scripts "
            "were checked against."
        )
        missing = [name for need, name in FEATURE_MIN_PROTOCOL if proto < need]
        if missing:
            status("  the node will likely reject: " + ", ".join(missing))
    elif proto > TESTED_PROTOCOL:
        status(
            f"! protocol {proto} is newer than the {TESTED_PROTOCOL} "
            f"({TESTED_FIRMWARE}) these scripts were checked against."
        )
        status(
            "  re-check the details they take from the firmware source — the "
            "factory-reset payload and the auto-add config bits — before trusting "
            "--reset or the auto-add report."
        )
    return info


def describe_node(mc: MeshCore) -> str:
    info = mc.self_info
    return (
        f"{info.get('name', '?')} ({info.get('public_key', '')[:12]}), "
        f"{info.get('radio_freq', '?')} MHz SF{info.get('radio_sf', '?')} "
        f"BW{info.get('radio_bw', '?')} CR{info.get('radio_cr', '?')}"
    )
