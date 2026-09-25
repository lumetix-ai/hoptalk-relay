#!/usr/bin/env python3
"""Lock down contact handling on the USB-attached MeshCore node.

By default it reports the node's state, switches every form of contact auto-add
off if any of it is on, and prints the node's own contact card.

    python node_setup.py                  # report, disable auto-add, print card
    python node_setup.py --check          # report only, exit 1 if auto-add is on
    python node_setup.py --wipe-contacts  # delete every stored contact (asks first)
    python node_setup.py --reset          # factory reset the node (asks first)
    python node_setup.py --setup          # first-run setup: name, radio, path hash, acks
    python node_setup.py --reboot         # reboot, reconnect, report what persisted
    python node_setup.py --discover-path KoalaBean   # measure the route to a contact

Two separate firmware settings decide whether an advert becomes a contact by
itself (companion_radio/MyMesh.cpp):

  * `manual_add_contacts` — when its low bit is 0, every advert is auto-added
    and the per-type bits below are not even consulted.
  * `autoadd_config` — consulted only in manual mode; bits 0x02/0x04/0x08/0x10
    still auto-add chat / repeater / room server / sensor nodes. Bit 0x01 is
    unrelated (overwrite the oldest contact when the table is full), so it is
    left as it is. `autoadd_max_hops` only narrows auto-add, never enables it.

Both are persisted on the node (savePrefs), so this has to be done once.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

from meshcore import EventType, MeshCore

from mcnode import (
    TESTED_PROTOCOL,
    add_connection_args,
    candidate_ports,
    check_firmware,
    configure_logging,
    connect,
    describe_node,
    describe_ports,
    fetch_contacts,
    resolve_port,
    status,
)

# CMD_FACTORY_RESET only fires when the literal "reset" follows the command byte
# (`memcmp(&cmd_frame[1], "reset", 5)` in MyMesh.cpp). The library's
# confirm_factory_reset() sends a bare 0x33, which that check rejects, so the
# frame is built here instead.
FACTORY_RESET_FRAME = b"\x33reset"

# Where the MeshCore clients get their radio presets: community-suggested
# settings served by MeshCore's own API, not something the node knows about.
PRESETS_URL = "https://api.meshcore.nz/api/v1/config"

# The node stores a path hash *mode*; a hop's hash is mode + 1 bytes, and the
# clients present it as the size in bytes.
PATH_HASH_SIZES = (1, 2, 3)

# "Direct Message Acks" in the web client: extra ACK transmissions per message.
DIRECT_MSG_ACKS = 2

# How long to wait for the USB serial port to come back after a reboot.
REBOOT_TIMEOUT = 30.0

# How long to wait for a path discovery answer. The firmware would suggest about
# five seconds for a flooded request; a distant contact behind repeaters can take
# a lot longer, and a late answer is still an answer.
DISCOVER_TIMEOUT = 30.0

AUTO_ADD_OVERWRITE_OLDEST = 0x01
AUTO_ADD_TYPE_BITS = {
    0x02: "chat",
    0x04: "repeater",
    0x08: "room server",
    0x10: "sensor",
}
AUTO_ADD_TYPE_MASK = 0x02 | 0x04 | 0x08 | 0x10


async def refresh_self_info(mc: MeshCore) -> None:
    """Re-read SELF_INFO so cached flags reflect what the node now holds."""
    await mc.commands.send_appstart()


async def read_autoadd(mc: MeshCore) -> tuple[bool, Optional[int], Optional[int]]:
    """(manual mode on?, autoadd_config, autoadd_max_hops)."""
    manual = bool(mc.self_info.get("manual_add_contacts"))
    res = await mc.commands.get_autoadd_config()
    if res is None or res.type == EventType.ERROR:
        # Firmware older than the per-type config: the manual flag is all there is.
        return manual, None, None
    return manual, res.payload.get("config"), res.payload.get("max_hops")


def autoadd_is_off(manual: bool, config: Optional[int]) -> bool:
    return manual and not (config or 0) & AUTO_ADD_TYPE_MASK


def report_autoadd(manual: bool, config: Optional[int], max_hops: Optional[int]) -> None:
    status(
        "  manual add contacts : "
        + ("on — adverts are not auto-added" if manual else "OFF — every advert is auto-added")
    )
    if config is None:
        status("  per-type auto-add   : not supported by this firmware")
        return
    types = [name for bit, name in AUTO_ADD_TYPE_BITS.items() if config & bit]
    status(f"  per-type auto-add   : {', '.join(types) if types else 'none'}")
    extras = []
    if config & AUTO_ADD_OVERWRITE_OLDEST:
        extras.append("overwrite oldest when full")
    if max_hops:
        extras.append(f"max {max_hops} hop(s)")
    elif max_hops == 0:
        extras.append("no hop limit")
    status(f"  raw config          : 0x{config:02x} ({', '.join(extras) or '-'})")


async def disable_autoadd(mc: MeshCore) -> bool:
    """Turn off both auto-add mechanisms; returns True if the node ends up clean."""
    manual, config, max_hops = await read_autoadd(mc)
    status("Contact auto-add:")
    report_autoadd(manual, config, max_hops)

    if autoadd_is_off(manual, config):
        status("Already off — nothing to change.")
        return True

    if not manual:
        res = await mc.commands.set_manual_add_contacts(True)
        if res is None or res.type == EventType.ERROR:
            status(f"! could not switch to manual add: {res.payload if res else 'no reply'}")
            return False
        status("+ switched contact adding to manual")

    if config is not None and config & AUTO_ADD_TYPE_MASK:
        # Only the type bits are cleared; the command sends one byte, so the
        # node keeps its max_hops setting.
        res = await mc.commands.set_autoadd_config(config & ~AUTO_ADD_TYPE_MASK)
        if res is None or res.type == EventType.ERROR:
            status(f"! could not clear the auto-add types: {res.payload if res else 'no reply'}")
            return False
        status("+ cleared per-type auto-add")

    await refresh_self_info(mc)
    manual, config, max_hops = await read_autoadd(mc)
    status("Now:")
    report_autoadd(manual, config, max_hops)
    if not autoadd_is_off(manual, config):
        status("! the node still reports auto-add as on")
        return False
    return True


async def show_card(mc: MeshCore, as_qr: bool = False) -> bool:
    """Print this node's own contact card, for importing on another device."""
    res = await mc.commands.export_contact()
    if res is not None and res.type == EventType.ERROR:
        # One retry: the node sometimes misses a command while it is still
        # streaming an earlier reply.
        res = await mc.commands.export_contact()
    if res is None or res.type == EventType.ERROR:
        status(f"Could not export the contact card: {res.payload if res else 'no reply'}")
        return False

    uri = res.payload.get("uri", "")
    status("Contact card for this node:")
    print(uri, flush=True)

    if as_qr:
        try:
            import qrcode  # optional, only needed for --qr
        except ImportError:
            status("QR output needs: .venv/bin/pip install qrcode")
            return True
        code = qrcode.QRCode(border=1)
        code.add_data(uri)
        code.print_ascii(invert=True)
    return True


async def wipe_contacts(mc: MeshCore, assume_yes: bool) -> bool:
    """Delete every contact stored on the node, after confirmation."""
    stored = await fetch_contacts(mc)
    if stored is None:
        return False
    contacts = list(stored.values())
    if not contacts:
        status("No contacts stored on the node.")
        return True

    status(f"{len(contacts)} contact(s) stored:")
    for c in contacts:
        status(f"  {c.get('adv_name') or '<unnamed>'} ({c.get('public_key', '')[:12]})")

    if not assume_yes:
        if not sys.stdin or not sys.stdin.isatty():
            status("Refusing to delete without a terminal to confirm on (use --yes).")
            return False
        answer = await asyncio.to_thread(
            input, f"Type 'delete' to remove all {len(contacts)} from the node: "
        )
        if answer.strip().lower() != "delete":
            status("Cancelled — nothing was deleted.")
            return False

    failed = 0
    for c in contacts:
        res = await mc.commands.remove_contact(c)
        if res is None or res.type == EventType.ERROR:
            failed += 1
            status(f"! could not delete {c.get('adv_name')}: {res.payload if res else 'no reply'}")

    # Ask the node what is left rather than trusting the local cache, which
    # only ever grows.
    remaining = await fetch_contacts(mc)
    left = -1 if remaining is None else len(remaining)
    status(f"Deleted {len(contacts) - failed} of {len(contacts)}; node now reports {left}.")
    return failed == 0 and left == 0



async def prompt(question: str, default: str = "") -> Optional[str]:
    """Ask on the terminal; None when there is nothing to ask on."""
    if not sys.stdin or not sys.stdin.isatty():
        status(f"No terminal to ask on — needed for: {question.strip()}")
        return None
    try:
        answer = await asyncio.to_thread(input, question)
    except (EOFError, RuntimeError):
        return None
    return answer.strip() or default


async def fetch_presets() -> list[dict[str, Any]]:
    """Radio presets as the MeshCore clients offer them."""

    def _get() -> Any:
        req = urllib.request.Request(PRESETS_URL, headers={"User-Agent": "hoptalk-relay"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)

    try:
        data = await asyncio.to_thread(_get)
        entries = data["config"]["suggested_radio_settings"]["entries"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        status(f"Could not fetch the preset list ({exc}); enter the radio settings by hand.")
        return []
    return [
        e
        for e in entries
        if {"title", "frequency", "bandwidth", "spreading_factor", "coding_rate"} <= e.keys()
    ]


def preset_matches(entry: dict[str, Any], info: dict[str, Any]) -> bool:
    try:
        return (
            float(entry["frequency"]) == float(info.get("radio_freq"))
            and float(entry["bandwidth"]) == float(info.get("radio_bw"))
            and int(entry["spreading_factor"]) == int(info.get("radio_sf"))
            and int(entry["coding_rate"]) == int(info.get("radio_cr"))
        )
    except (TypeError, ValueError):
        return False


def find_preset(entries: list[dict[str, Any]], wanted: str) -> Optional[dict[str, Any]]:
    want = wanted.strip().lower()
    for entry in entries:
        if entry["title"].lower() == want:
            return entry
    partial = [e for e in entries if want in e["title"].lower()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        status("That matches several presets: " + ", ".join(e["title"] for e in partial))
    else:
        status(f"No preset called {wanted!r}.")
    return None


async def choose_radio(
    info: dict[str, Any], wanted: Optional[str]
) -> Optional[dict[str, Any]]:
    """Pick a preset, or build one from hand-entered values."""
    entries = await fetch_presets()

    if wanted:
        return find_preset(entries, wanted) if entries else None

    if entries:
        status("Radio presets:")
        for i, entry in enumerate(entries, 1):
            mark = "  <- what the node uses now" if preset_matches(entry, info) else ""
            status(f"  {i:2d}. {entry['title']} — {entry['description']}{mark}")
    status("   0. enter frequency/bandwidth/SF/CR by hand")

    answer = await prompt("Preset number: ")
    if answer is None:
        return None
    if answer.isdigit() and 1 <= int(answer) <= len(entries):
        return entries[int(answer) - 1]
    if answer != "0":
        status("Not one of the listed numbers.")
        return None

    fields = {}
    for key, question, current in (
        ("frequency", "Frequency in MHz", info.get("radio_freq")),
        ("bandwidth", "Bandwidth in kHz", info.get("radio_bw")),
        ("spreading_factor", "Spreading factor (5-12)", info.get("radio_sf")),
        ("coding_rate", "Coding rate (5-8)", info.get("radio_cr")),
    ):
        value = await prompt(f"{question} [{current}]: ", default=str(current))
        if value is None:
            return None
        fields[key] = value
    fields["title"] = "custom"
    fields["description"] = (
        f"{fields['frequency']}MHz / SF{fields['spreading_factor']} / "
        f"BW{fields['bandwidth']} / CR{fields['coding_rate']}"
    )
    return fields


async def current_path_hash_size(mc: MeshCore) -> Optional[int]:
    """Path hash size the node reports, or None on firmware that omits it."""
    res = await mc.commands.send_device_query()
    if res is None or res.type == EventType.ERROR:
        return None
    mode = res.payload.get("path_hash_mode")
    return None if mode is None else mode + 1


async def setup_node(mc: MeshCore, args: argparse.Namespace) -> bool:
    """First-run setup, in the order the web client walks through it."""
    info = mc.self_info

    # 1. Name
    name = args.name or await prompt(f"Node name [{info.get('name', '')}]: ",
                                     default=info.get("name", ""))
    if not name:
        status("Setup needs a name.")
        return False
    if name != info.get("name"):
        res = await mc.commands.set_name(name)
        if res is None or res.type == EventType.ERROR:
            status(f"! could not set the name: {res.payload if res else 'no reply'}")
            return False
        status(f"+ name set to {name!r}")
    else:
        status(f"= name already {name!r}")

    # 2. Radio settings, from the preset list the clients use
    preset = await choose_radio(info, args.preset)
    if preset is None:
        return False
    freq = float(preset["frequency"])
    bw = float(preset["bandwidth"])
    sf = int(preset["spreading_factor"])
    cr = int(preset["coding_rate"])
    res = await mc.commands.set_radio(freq, bw, sf, cr)
    if res is None or res.type == EventType.ERROR:
        status(f"! could not set the radio: {res.payload if res else 'no reply'}")
        return False
    status(f"+ radio set to {preset['title']}: {freq} MHz SF{sf} BW{bw} CR{cr}")

    # 3. Default path hash size — some presets carry the one their region uses
    suggested = (preset.get("network_settings") or {}).get("path_hash_size")
    current = await current_path_hash_size(mc)
    default = suggested or current or 1
    if args.path_hash_size:
        size = args.path_hash_size
    else:
        where = "from the preset" if suggested else "current"
        answer = await prompt(
            f"Default path hash size in bytes {PATH_HASH_SIZES} [{default} — {where}]: ",
            default=str(default),
        )
        if answer is None:
            return False
        if not answer.isdigit() or int(answer) not in PATH_HASH_SIZES:
            status(f"Path hash size has to be one of {PATH_HASH_SIZES}.")
            return False
        size = int(answer)
    res = await mc.commands.set_path_hash_mode(size - 1)
    if res is None or res.type == EventType.ERROR:
        status(f"! could not set the path hash size: {res.payload if res else 'no reply'}")
        return False
    status(f"+ default path hash size set to {size} byte(s)")

    # 4. Direct message acks — fixed, no question asked
    res = await mc.commands.set_multi_acks(DIRECT_MSG_ACKS)
    if res is None or res.type == EventType.ERROR:
        status(f"! could not set direct message acks: {res.payload if res else 'no reply'}")
        return False
    status(f"+ direct message acks set to {DIRECT_MSG_ACKS}")

    # 5. Auto-add is handled by the usual disable_autoadd() step that follows.
    await refresh_self_info(mc)
    status(f"Node is now: {describe_node(mc)}")
    return True


def format_path(path_hex: str, hop_bytes: int) -> str:
    """Render a path as its per-hop hashes, in travel order."""
    if not path_hex:
        return "no hops — a direct neighbour"
    width = max(hop_bytes, 1) * 2
    hops = [path_hex[i : i + width] for i in range(0, len(path_hex), width)]
    return " -> ".join(hops)


async def find_contact(mc: MeshCore, wanted: str) -> Optional[dict[str, Any]]:
    """Look a contact up by name or public-key prefix."""
    stored = await fetch_contacts(mc)
    if stored is None:
        return None

    want = wanted.strip().lower()
    for contact in stored.values():
        if (contact.get("adv_name") or "").lower() == want:
            return contact

    partial = [
        c
        for c in stored.values()
        if want in (c.get("adv_name") or "").lower()
        or c.get("public_key", "").lower().startswith(want)
    ]
    if len(partial) == 1:
        return partial[0]
    if partial:
        status("That matches several contacts: " + ", ".join(
            c.get("adv_name") or "<unnamed>" for c in partial
        ))
    else:
        known = ", ".join(c.get("adv_name") or "<unnamed>" for c in stored.values())
        status(f"No contact matching {wanted!r}. Stored: {known or 'none'}")
    return None


async def discover_path(mc: MeshCore, wanted: str, timeout: float) -> bool:
    """Ask the mesh for the route to a contact and report both directions.

    The firmware forces this request to flood (it clears out_path for the send,
    CMD_SEND_PATH_DISCOVERY_REQ in MyMesh.cpp), so an answer proves that our
    packets reach that node *and* that its reply finds its way back. Silence
    means one of the two legs failed, without saying which.

    It only measures: the firmware reports the paths to us and deliberately does
    not store them ("DON'T send reciprocal path!"), so this changes no routing.
    """
    contact = await find_contact(mc, wanted)
    if contact is None:
        return False

    name = contact.get("adv_name") or "<unnamed>"
    key = contact.get("public_key", "")
    stored = contact.get("out_path_len", -1)
    status(f"Path discovery to {name} ({key[:12]}), up to {timeout:.0f}s:")
    status(
        "  the node currently stores: "
        + ("no route, so it floods" if stored < 0 else f"a {stored}-hop route")
    )

    res = await mc.commands.send_path_discovery_sync(contact, timeout=timeout)
    if res is None:
        status(f"  no answer within {timeout:.0f}s — one of the two legs did not make it.")
        status("  Which one is still open: retry from closer and compare.")
        return False

    payload = res.payload
    out_hops, in_hops = payload["out_path_len"], payload["in_path_len"]
    status(
        f"  us -> them : {out_hops} hop(s)   "
        f"{format_path(payload['out_path'], payload['out_path_hash_len'])}"
    )
    status(
        f"  them -> us : {in_hops} hop(s)   "
        f"{format_path(payload['in_path'], payload['in_path_hash_len'])}"
    )
    if out_hops != in_hops:
        status("  The directions differ, so the mesh is not routing this symmetrically.")
    return True


async def wait_for_port(port: str, timeout: float = REBOOT_TIMEOUT) -> Optional[str]:
    """Wait for the node's serial port to come back after a reboot."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    # Let it drop off the bus first, but do not hang here if the reboot is so
    # quick (or the OS so slow) that the device node never visibly disappears.
    drop_deadline = min(deadline, loop.time() + 5.0)
    while loop.time() < drop_deadline and port in candidate_ports():
        await asyncio.sleep(0.5)

    while loop.time() < deadline:
        ports = candidate_ports()
        if port in ports:
            await asyncio.sleep(1.0)  # the CDC endpoint needs a beat
            return port
        if ports:
            await asyncio.sleep(1.0)
            status(f"Port came back as {ports[0]} (was {port}).")
            return ports[0]
        await asyncio.sleep(0.5)
    return None


async def reboot_node(
    mc: MeshCore, args: argparse.Namespace, port: str
) -> Optional[MeshCore]:
    """Reboot and reopen the node, so the caller can see what survived.

    None of the settings this script writes needs a reboot — the firmware
    applies each one immediately and calls savePrefs() — so this is a check that
    they really persisted, not a step that makes them take effect.
    """
    status("Rebooting the node ...")
    # The firmware requires the literal "reboot" and does not answer; the
    # library's reboot() already sends the right frame.
    await mc.commands.reboot()
    try:
        await mc.disconnect()
    except Exception as exc:  # the link is already going away
        logging.getLogger("node_setup").debug("disconnect during reboot: %s", exc)

    found = await wait_for_port(port)
    if found is None:
        status(f"The node did not reappear on {port} within {REBOOT_TIMEOUT:.0f}s.")
        return None
    return await connect(args, port=found, default_timeout=10)


async def factory_reset(
    mc: MeshCore, assume_yes: bool, firmware: Optional[dict[str, Any]] = None
) -> bool:
    """Erase everything on the node and reboot it. Not reversible."""
    info = mc.self_info
    name = info.get("name") or ""

    if (firmware or {}).get("fw ver") != TESTED_PROTOCOL:
        # The reset only fires when the payload matches what the firmware's
        # memcmp expects; a firmware that changed it would ignore the command
        # without complaining, and "no reply" is what success looks like here.
        status(
            "! this node's protocol is not the one the reset frame was verified "
            f"against ({TESTED_PROTOCOL}). If this firmware expects a different "
            "payload, the node will ignore the command and the script cannot tell "
            "that apart from a successful reset. Check the node afterwards."
        )

    status("A factory reset formats the node's filesystem. What that means:")
    status(f"  identity : {info.get('public_key', '')[:12]}… is replaced by a NEW random key")
    status(f"  name     : {name!r} → the firmware's default")
    status(
        f"  radio    : {info.get('radio_freq', '?')} MHz "
        f"SF{info.get('radio_sf', '?')} BW{info.get('radio_bw', '?')} "
        f"CR{info.get('radio_cr', '?')} → the firmware build's defaults, which may "
        "not match your other nodes"
    )
    status("  contacts, channels and every setting are erased")
    status("  contact auto-add comes back ON — that is the factory default")
    status(
        "Every other device keeps a contact entry pointing at the OLD key, so all "
        "pairing has to be redone from both sides."
    )

    if not assume_yes:
        if not sys.stdin or not sys.stdin.isatty():
            status("Refusing to reset without a terminal to confirm on (use --yes).")
            return False
        prompt = f"Type the node's name ({name!r}) to confirm the reset: "
        answer = await asyncio.to_thread(input, prompt)
        if answer.strip() != name:
            status("Cancelled — the node was not touched.")
            return False

    res = await mc.commands.send(
        FACTORY_RESET_FRAME, [EventType.OK, EventType.ERROR], timeout=15
    )
    payload = (res.payload if res is not None else None) or {}
    if res is not None and res.type == EventType.OK:
        status("Node accepted the reset and is rebooting.")
    elif "error_code" in payload:
        status(f"! the node rejected the reset: {payload}")
        return False
    else:
        # Expected on current firmware: it disables the serial interface before
        # formatting, so the OK frame usually never makes it out.
        status(
            "No confirmation frame — expected, the firmware closes the serial "
            "link before it can answer. The node should be formatting now."
        )

    status("Give it ~10 s to reboot (the serial port re-enumerates), then run")
    status("  node_setup.py        to turn auto-add back off,")
    status("  listen_dm.py --pair  to pair your pagers with the new key.")
    return True


async def run(args: argparse.Namespace) -> int:
    port = resolve_port(args)
    if port is None:
        return 1

    mc = await connect(args, port=port, default_timeout=10)
    if mc is None:
        return 1

    # --setup is a first-run ceremony, so it verifies itself across a reboot
    # unless told not to.
    reboot_wanted = args.reboot or (args.setup and not args.no_reboot)

    ok = True
    try:
        status(f"Connected: {describe_node(mc)}")
        firmware = await check_firmware(mc)
        stored = await fetch_contacts(mc)
        if stored is None:
            ok = False
        status(f"Contacts stored: {'?' if stored is None else len(stored)}")
        for c in (stored or {}).values():
            status(f"  {c.get('adv_name') or '<unnamed>'} ({c.get('public_key', '')[:12]})")

        if args.discover_path:
            # Diagnostic only, and it writes nothing, so it runs on its own.
            return 0 if await discover_path(mc, args.discover_path, args.discover_timeout) else 1

        if args.reset:
            # Nothing else is worth doing: the reset erases whatever we would
            # have configured, and the node reboots straight after.
            return 0 if await factory_reset(mc, args.yes, firmware) else 1

        if args.wipe_contacts:
            ok = await wipe_contacts(mc, args.yes) and ok

        if args.setup:
            ok = await setup_node(mc, args) and ok

        if args.check:
            manual, config, max_hops = await read_autoadd(mc)
            status("Contact auto-add:")
            report_autoadd(manual, config, max_hops)
            if not autoadd_is_off(manual, config):
                status("Auto-add is ON — run without --check to turn it off.")
                ok = False
        else:
            ok = await disable_autoadd(mc) and ok

        if reboot_wanted:
            rebooted = await reboot_node(mc, args, port)
            if rebooted is None:
                status("! could not reopen the node — replug it and run --check.")
                return 1
            mc = rebooted
            status(f"Back up: {describe_node(mc)}")
            manual, config, max_hops = await read_autoadd(mc)
            report_autoadd(manual, config, max_hops)
            size = await current_path_hash_size(mc)
            status(f"  path hash size      : {size or 'not reported by this firmware'}")
            status(f"  direct message acks : {mc.self_info.get('multi_acks', '?')}")
            ok = autoadd_is_off(manual, config) and ok

        if not args.no_card:
            ok = await show_card(mc, as_qr=args.qr) and ok
    finally:
        try:
            await mc.disconnect()
        except Exception as exc:  # the node may already be rebooting
            logging.getLogger("node_setup").debug("disconnect failed: %s", exc)

    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Turn off contact auto-add on the node, show its contact card, "
        "and optionally wipe its contacts."
    )
    add_connection_args(parser)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report the auto-add state without changing it (exit 1 if it is on)",
    )
    parser.add_argument(
        "--wipe-contacts",
        action="store_true",
        help="delete every contact stored on the node (asks for confirmation)",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="first-run setup: node name, radio preset, default path hash size, "
        "direct message acks, and contact auto-add off",
    )
    parser.add_argument(
        "--name", help="node name for --setup (skips that prompt)"
    )
    parser.add_argument(
        "--preset", help="radio preset title for --setup (skips that prompt)"
    )
    parser.add_argument(
        "--path-hash-size",
        type=int,
        choices=PATH_HASH_SIZES,
        help="default path hash size in bytes for --setup (skips that prompt)",
    )
    parser.add_argument(
        "--discover-path",
        metavar="CONTACT",
        help="measure the route to a contact (by name or public-key prefix) and "
        "report both directions; changes nothing",
    )
    parser.add_argument(
        "--discover-timeout",
        type=float,
        default=DISCOVER_TIMEOUT,
        metavar="SECONDS",
        help="how long to wait for the discovery answer (default: %(default)s)",
    )
    parser.add_argument(
        "--reboot",
        action="store_true",
        help="reboot the node afterwards, reconnect, and report what persisted",
    )
    parser.add_argument(
        "--no-reboot",
        action="store_true",
        help="skip the reboot that --setup does by default",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="factory reset the node: erases its identity, contacts, channels and "
        "settings, then reboots it (asks for confirmation)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt for --wipe-contacts / --reset",
    )
    parser.add_argument(
        "--qr", action="store_true", help="also render the contact card as a QR code"
    )
    parser.add_argument(
        "--no-card", action="store_true", help="do not print the contact card"
    )
    args = parser.parse_args()
    configure_logging(args.debug)

    if args.list_ports:
        print(describe_ports())
        return 0

    if args.reset and (args.check or args.wipe_contacts or args.setup):
        status("--reset cannot be combined with --check, --wipe-contacts or --setup.")
        return 2
    if args.setup and args.check:
        status("--setup writes settings, so it cannot be combined with --check.")
        return 2
    if args.reset and args.reboot:
        status("--reset reboots the node by itself.")
        return 2
    if args.discover_path and (args.reset or args.setup or args.wipe_contacts):
        status("--discover-path only measures; run it on its own.")
        return 2

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
