#!/usr/bin/env python3
"""Listen to a USB-attached MeshCore node, print incoming DMs and acknowledge them.

Every plain direct message gets a reply (RECEIVED by default); --no-reply turns
that off, --reply-text changes the wording.

    python listen_dm.py                            # auto-detect the serial port
    python listen_dm.py --list-ports
    python listen_dm.py --port /dev/cu.usbmodem1101 --raw

Messages go to stdout, status/diagnostics to stderr, so the stream can be piped.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import fnmatch
import logging
import secrets
import signal
import sys
from collections import deque
from datetime import datetime
from typing import Any, Optional

from meshcore import EventType, MeshCore

from mcnode import (
    add_connection_args,
    check_firmware,
    configure_logging,
    connect,
    describe_node,
    describe_ports,
    fetch_contacts,
    status,
)

TXT_TYPES = {0: "plain", 1: "cli", 2: "signed"}

# What gets an automatic reply: plain text and signed plain text. CLI data
# (txt_type 1) is a remote-administration channel, not a conversation.
REPLYABLE_TXT_TYPES = (0, 2)

DEFAULT_REPLY_TEXT = "RECEIVED"

# How many (sender, timestamp, text) triples to remember, to recognise a sender
# repeating itself instead of answering every attempt.
RETRY_MEMORY = 200

# The node pushes a "messages waiting" notification for every new message, so
# this is only a safety net in case one is missed (e.g. during a reconnect).
POLL_INTERVAL = 30.0

# How many channel slots to probe; the firmware answers ERROR past its last one.
CHANNEL_SLOTS = 8

# Default length of the --pair window, in seconds.
PAIR_TIMEOUT = 120.0

log = logging.getLogger("listen_dm")


def fmt_clock(unix_ts: Optional[int]) -> str:
    """Format a unix timestamp in local time; tolerate unset/bogus values."""
    if not unix_ts:
        return "?"
    try:
        dt = datetime.fromtimestamp(unix_ts)
    except (OverflowError, OSError, ValueError):
        return f"raw:{unix_ts}"
    # This is the sender's clock, which may be days off - show the date too,
    # so a stale timestamp does not read as "a moment ago".
    if dt.date() == datetime.now().date():
        return dt.strftime("%H:%M:%S")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def route_info(msg: dict[str, Any]) -> str:
    """Short summary of how the message reached us."""
    bits = []
    # On receive, 0xFF means the packet came in over a direct route; any other
    # value is the hop count of a flooded packet (the sense is inverted on the
    # send side, where 0xFF means "no known path, use flood").
    path_len = msg.get("path_len")
    if path_len == 255:
        bits.append("direct")
    elif path_len is not None:
        bits.append(f"flood, {path_len} hop" + ("s" if path_len != 1 else ""))
    snr = msg.get("SNR")
    if snr is not None:
        bits.append(f"SNR {snr}")
    txt_type = msg.get("txt_type", 0)
    if txt_type != 0:
        bits.append(TXT_TYPES.get(txt_type, f"txt_type={txt_type}"))
    sent = fmt_clock(msg.get("sender_timestamp"))
    if sent != "?":
        bits.append(f"sent {sent}")
    return ", ".join(bits)


def contact_matches(contact: dict[str, Any], patterns: list[str]) -> bool:
    """True if a contact matches an --accept pattern (name glob or key prefix)."""
    name = (contact.get("adv_name") or "").lower()
    key = (contact.get("public_key") or "").lower()
    for raw in patterns:
        pattern = raw.lower()
        if not pattern:
            continue
        if fnmatch.fnmatch(name, pattern):
            return True
        if all(c in "0123456789abcdef" for c in pattern) and key.startswith(pattern):
            return True
    return False


class DirectMessageListener:
    """Drains the node's message queue and prints every DM it yields.

    The firmware handles one command at a time, so every device command is
    issued from `run()` only.  Event handlers just do bookkeeping — that keeps
    two commands from being in flight on the serial link at once.
    """

    def __init__(
        self,
        mc: MeshCore,
        show_raw: bool = False,
        show_adverts: bool = False,
        accept_all: bool = False,
        accept_patterns: Optional[list[str]] = None,
        ask: bool = False,
        channels: Optional[dict[int, str]] = None,
        pair: bool = False,
        pair_key: Optional[str] = None,
        pair_timeout: float = PAIR_TIMEOUT,
        reply_text: Optional[str] = DEFAULT_REPLY_TEXT,
    ) -> None:
        self.mc = mc
        self.show_raw = show_raw
        self.accept_all = accept_all
        self.accept_patterns = accept_patterns or []
        self.ask = ask
        self.reply_text = reply_text  # None = never reply
        self.pair_key = (pair_key or "").lower()
        self.pair_timeout = pair_timeout
        self.pair_deadline: Optional[float] = None  # set when the window opens
        self.paired = False
        self._explained = False  # a decision already printed its own reason
        self.show_adverts = (
            show_adverts or accept_all or bool(self.accept_patterns) or ask or pair
        )
        # idx -> channel name, for labelling channel messages; None = DM only
        self.channels = channels
        self.inbox: deque[dict[str, Any]] = deque()
        self._seen: set[tuple] = set()             # messages already answered
        self._seen_order: deque[tuple] = deque()   # same keys, oldest first
        # (public_key, pending contact dict or None if the node already knows it)
        self.adverts: deque[tuple[str, Optional[dict[str, Any]]]] = deque()
        self.wake = asyncio.Event()
        self.stopped = False
        self.lost_connection = False
        self._refreshed = False  # contacts re-synced during the current pass

    def subscribe(self) -> None:
        self.mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_dm)
        self.mc.subscribe(EventType.MESSAGES_WAITING, self._on_messages_waiting)
        self.mc.subscribe(EventType.CONNECTED, self._on_connected)
        self.mc.subscribe(EventType.DISCONNECTED, self._on_disconnected)
        if self.channels is not None:
            self.mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)
        if self.show_adverts:
            # Heard an advert from a node the radio auto-added / already knows.
            self.mc.subscribe(EventType.ADVERTISEMENT, self._on_advert)
            # Same, but auto-add is off for that node type, so it stays pending.
            self.mc.subscribe(EventType.NEW_CONTACT, self._on_new_contact)

    def open_pairing_window(self) -> None:
        self.pair_deadline = asyncio.get_running_loop().time() + self.pair_timeout
        target = f"key {self.pair_key}" if self.pair_key else "the first node you confirm"
        status(
            f"Pairing window open for {self.pair_timeout:.0f}s — waiting for an "
            f"advert from {target}. Press 'Advert' on the other device now."
        )

    def pairing_open(self) -> bool:
        if self.pair_deadline is None or self.paired:
            return False
        return asyncio.get_running_loop().time() < self.pair_deadline

    def _check_pairing_expiry(self) -> None:
        if self.pair_deadline is None or self.paired:
            return
        if asyncio.get_running_loop().time() >= self.pair_deadline:
            self.pair_deadline = None
            status("Pairing window closed — nothing was added.")

    def stop(self) -> None:
        self.stopped = True
        self.wake.set()

    # ── event handlers (no device commands in here) ──────────────────────
    def _on_dm(self, event) -> None:
        self.inbox.append(event.payload)

    def _on_messages_waiting(self, event) -> None:
        self.wake.set()

    def _on_channel_msg(self, event) -> None:
        self.inbox.append(event.payload)

    def _on_advert(self, event) -> None:
        self.adverts.append((event.payload.get("public_key", ""), None))
        self.wake.set()

    def _on_new_contact(self, event) -> None:
        self.adverts.append((event.payload.get("public_key", ""), event.payload))
        self.wake.set()

    def _on_connected(self, event) -> None:
        if event.payload.get("reconnected"):
            status("~ reconnected, checking for missed messages")
            self.wake.set()

    def _on_disconnected(self, event) -> None:
        self.lost_connection = True
        status(f"! disconnected: {event.payload.get('reason', 'unknown')}")
        self.stop()

    # ── main loop ────────────────────────────────────────────────────────
    async def run(self) -> None:
        while not self.stopped:
            # Clear before draining: a "messages waiting" push that lands
            # mid-drain then triggers one extra (harmless) drain instead of
            # being swallowed.
            self.wake.clear()
            self._refreshed = False
            await self._drain()
            await self._handle_adverts()
            await self._print_inbox()
            if self.stopped:
                break
            timeout = POLL_INTERVAL
            if self.pairing_open():
                remaining = self.pair_deadline - asyncio.get_running_loop().time()
                timeout = max(1.0, min(POLL_INTERVAL, remaining))
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass  # periodic re-check in case a push notification was lost
            self._check_pairing_expiry()

    async def _drain(self) -> None:
        """Pull queued messages until the node says there are no more."""
        while not self.stopped:
            res = await self.mc.commands.get_msg()
            if res is None or res.type == EventType.NO_MORE_MSGS:
                return
            if res.type == EventType.ERROR:
                log.debug("get_msg failed: %s", res.payload)
                return

    def _should_reply(self, msg: dict[str, Any]) -> bool:
        if not self.reply_text or msg.get("type") == "CHAN":
            return False
        if msg.get("txt_type", 0) not in REPLYABLE_TXT_TYPES:
            return False
        if not msg.get("pubkey_prefix"):
            return False
        # Never answer our own wording coming back: two nodes both replying
        # would otherwise keep each other busy forever.
        return msg.get("text", "").strip() != self.reply_text

    async def _reset_path(self, contact: dict[str, Any], sender: str, why: str) -> bool:
        """Clear the node's stored route to a contact, so the next send floods."""
        res = await self.mc.commands.reset_path(contact)
        if res is None or res.type == EventType.ERROR:
            detail = res.payload if res else "no reply from the node"
            status(f"! could not clear the path to {sender}: {detail}")
            return False
        status(f"~ cleared the stored path to {sender} ({why}); the next send floods")
        return True

    async def _send_reply(self, target: Any, sender: str) -> tuple[bool, bool, str]:
        """Send the acknowledgement once. Returns (sent, acked, route)."""
        loop = asyncio.get_running_loop()
        acked: asyncio.Future = loop.create_future()
        seen: set[str] = set()
        expected: Optional[str] = None

        def on_ack(event) -> None:
            code = event.attributes.get("code")
            seen.add(code)
            if expected is not None and code == expected and not acked.done():
                acked.set_result(code)

        # Subscribe before sending: the ack can be queued right behind MSG_SENT.
        sub = self.mc.subscribe(EventType.ACK, on_ack)
        try:
            res = await self.mc.commands.send_msg(target, self.reply_text)
            if res is None or res.type == EventType.ERROR:
                detail = res.payload if res else "no reply from the node"
                status(f"! reply to {sender} not sent: {detail}")
                return False, False, ""

            expected = res.payload.get("expected_ack", b"").hex()
            route = "flood" if res.payload.get("type") == 1 else "direct"
            # The node suggests how long its own attempt may take, in ms.
            timeout = (res.payload.get("suggested_timeout") or 0) / 1000 * 1.2
            if expected not in seen:
                try:
                    await asyncio.wait_for(acked, timeout=max(timeout, 2.0))
                except asyncio.TimeoutError:
                    status(f"-> {sender}: {self.reply_text} ({route}, no ack yet)")
                    return True, False, route
            status(f"-> {sender}: {self.reply_text} ({route}, acked)")
            return True, True, route
        finally:
            self.mc.unsubscribe(sub)

    async def _reply(
        self, msg: dict[str, Any], contact: Optional[dict[str, Any]], sender: str
    ) -> None:
        """Acknowledge a DM, clearing a stale route when the reply goes unheard.

        The firmware sends along a contact's stored out_path whenever it has one
        and never falls back to flood by itself, and its own acks go the same way
        (BaseChatMesh::sendMessage, sendAckTo). A path learned while the other
        node was nearby therefore keeps swallowing both until it is cleared, and
        nothing refreshes it: the node only learns a new path when the other side
        returns one, which it only does after receiving something by flood.
        """
        target: Any = contact or msg["pubkey_prefix"]
        has_path = bool(contact) and contact.get("out_path_len", -1) >= 0

        # It reached us by flood, so the sender had no working path here. Ours
        # back to them was learned earlier and is just as likely dead.
        if has_path and msg.get("path_len") != 255:
            if await self._reset_path(contact, sender, "it reached us by flood"):
                has_path = False

        sent, acked, route = await self._send_reply(target, sender)
        if not sent or acked:
            return

        if has_path and route != "flood":
            if await self._reset_path(contact, sender, "no ack for the reply"):
                await self._send_reply(contact, sender)

    async def _resolve_contact(self, key: str) -> Optional[dict[str, Any]]:
        """The contact for a public key (or prefix), re-syncing once if needed."""
        if not key:
            return None
        contact = self.mc.get_contact_by_key_prefix(key)
        if contact is None and not self._refreshed:
            # First traffic from a contact added after we synced the list.
            self._refreshed = True
            await fetch_contacts(self.mc)
            contact = self.mc.get_contact_by_key_prefix(key)
        return contact

    async def _resolve_name(self, key: str) -> Optional[str]:
        contact = await self._resolve_contact(key)
        return contact.get("adv_name") if contact else None

    def _is_retry(self, msg: dict[str, Any]) -> bool:
        """True when the sender is repeating a message we have already handled.

        Every attempt carries the sender's original timestamp — the attempt
        number is what makes the packet unique — so this triple identifies one
        message however many times it arrives.
        """
        if msg.get("type") == "CHAN":
            return False
        key = (msg.get("pubkey_prefix"), msg.get("sender_timestamp"), msg.get("text"))
        if key in self._seen:
            return True
        self._seen.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > RETRY_MEMORY:
            self._seen.discard(self._seen_order.popleft())
        return False

    async def _should_add(self, pending: dict[str, Any], name: str, key: str) -> bool:
        """Decide whether a node that just advertised belongs in the contacts."""
        if self.accept_all:
            return True
        if contact_matches(pending, self.accept_patterns):
            status(f"~ {name} ({key[:12]}) matches --accept")
            return True
        if self.pairing_open():
            if self.pair_key:
                if key.lower().startswith(self.pair_key):
                    status(f"~ {name} matches the key given to --pair")
                    self.paired = True
                    return True
                status(f"~ ignoring {name} ({key[:12]}): not the key being paired")
                self._explained = True  # do not repeat the generic pending line
                return False
            # No key given: the operator compares the full key with the one the
            # other device shows on screen, which is the part worth checking -
            # the name is whatever that node claims it is.
            if await self._confirm(f"?  pair with {name}?\n   key: {key}\n   [y/N] "):
                self.paired = True
                return True
            return False

        if self.ask:
            return await self._confirm(f"?  add {name} ({key[:12]})? [y/N] ")
        return False

    async def _confirm(self, prompt: str) -> bool:
        if not sys.stdin or not sys.stdin.isatty():
            status("~ no terminal on stdin; leaving the contact pending")
            return False
        try:
            answer = await asyncio.to_thread(input, prompt)
        except (EOFError, RuntimeError):
            return False
        return answer.strip().lower() in ("y", "yes")

    async def _handle_adverts(self) -> None:
        while self.adverts:
            key, pending = self.adverts.popleft()
            if pending is None:
                name = await self._resolve_name(key) or "<unnamed>"
                status(f"~ advert heard from {name} ({key[:12]})")
                continue

            # The node did not auto-add this one, so it has no shared secret for
            # it yet - a DM from that node cannot be decrypted or ACKed.
            name = pending.get("adv_name") or "<unnamed>"
            self._explained = False
            if not await self._should_add(pending, name, key):
                if not self._explained:
                    status(
                        f"~ advert heard from {name} ({key[:12]})  — pending, not "
                        "in contacts; DMs from it cannot be decrypted"
                    )
                continue

            res = await self.mc.commands.add_contact(pending)
            if res is None or res.type == EventType.ERROR:
                detail = res.payload if res else "no reply"
                status(f"! could not add {name} ({key[:12]}): {detail}")
                continue
            self.mc.pop_pending_contact(key)
            await fetch_contacts(self.mc)
            status(f"+ added contact {name} ({key[:12]}) — its DMs can now be read")
            if self.paired:
                self.pair_deadline = None
                status("Pairing done. Send a DM from that device to test it.")

    async def _print_inbox(self) -> None:
        while self.inbox:
            msg = self.inbox.popleft()
            contact = None
            if msg.get("type") == "CHAN":
                idx = msg.get("channel_idx")
                # A channel message carries no sender key - whatever name it
                # shows is plain text the sending client put in the message.
                sender = f"#{(self.channels or {}).get(idx) or idx}"
            else:
                prefix = msg.get("pubkey_prefix", "")
                contact = await self._resolve_contact(prefix)
                sender = (contact or {}).get("adv_name") or f"<{prefix or 'unknown'}>"

            retry = self._is_retry(msg)
            text = msg.get("text", "")
            route = route_info(msg)
            if retry:
                route = f"{route}, retry" if route else "retry"
            suffix = f"   ({route})" if route else ""
            print(f"[{datetime.now():%H:%M:%S}] {sender}: {text}{suffix}", flush=True)
            if self.show_raw:
                print(f"           raw: {msg}", flush=True)

            # One reply per message, not per attempt: the sender repeating itself
            # means our answers are not getting through, so more of them into the
            # same dead route would only add airtime.
            if not retry and self._should_reply(msg):
                await self._reply(msg, contact, sender)


async def channel_table(mc: MeshCore) -> dict[int, dict[str, Any]]:
    """Read the configured channel slots off the node."""
    table: dict[int, dict[str, Any]] = {}
    for idx in range(CHANNEL_SLOTS):
        res = await mc.commands.get_channel(idx)
        if res is None or res.type == EventType.ERROR:
            break
        table[idx] = res.payload
    return table


def channel_is_empty(info: dict[str, Any]) -> bool:
    return not info.get("channel_name") or info.get("channel_secret") == bytes(16)


async def create_channel(mc: MeshCore, name: str) -> Optional[int]:
    """Create a private channel with a fresh random key in the first free slot."""
    if name.startswith("#"):
        status(
            "A channel name starting with '#' makes the key a hash of the name, "
            "so anyone who knows the name can read it. Pick another name."
        )
        return None

    table = await channel_table(mc)
    free = [idx for idx, info in table.items() if channel_is_empty(info)]
    if not free:
        status("No free channel slot on the node:")
        for idx, info in table.items():
            status(f"  {idx}: {info.get('channel_name')}")
        status("Free one in the MeshCore client first — this script never overwrites.")
        return None

    idx = free[0]
    secret = secrets.token_bytes(16)
    res = await mc.commands.set_channel(idx, name, secret)
    if res is None or res.type == EventType.ERROR:
        status(f"Could not create the channel: {res.payload if res else 'no reply'}")
        return None

    status(f"Created channel '{name}' in slot {idx}. Add it on the other device with:")
    print(f"  channel name : {name}")
    print(f"  key (hex)    : {secret.hex()}")
    print(f"  key (base64) : {base64.b64encode(secret).decode()}")
    return idx


async def import_card(mc: MeshCore, card: str) -> bool:
    """Add a contact from a `meshcore://…` card, without waiting for an advert."""
    text = card.strip()
    if text.startswith("meshcore://"):
        text = text[len("meshcore://") :]
    text = "".join(text.split())
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        status("That is not a contact card — expected meshcore://<hex>.")
        return False

    known = set(mc.contacts)
    res = await mc.commands.import_contact(raw)
    if res is None or res.type == EventType.ERROR:
        status(f"Import failed: {res.payload if res else 'no reply'}")
        return False

    # Diff the contact list rather than parsing the card: the node is the one
    # that decides what it stored.
    await fetch_contacts(mc)
    added = [c for key, c in mc.contacts.items() if key not in known]
    if added:
        c = added[0]
        status(
            f"+ imported {c.get('adv_name') or 'contact'} "
            f"({c.get('public_key', '')[:12]}) — its DMs can now be read"
        )
    else:
        status("+ card accepted (contact list unchanged — it was already known)")
    return True


async def run(args: argparse.Namespace) -> int:
    mc = await connect(
        args, default_timeout=10, auto_reconnect=True, max_reconnect_attempts=5
    )
    if mc is None:
        return 1

    loop = asyncio.get_running_loop()

    try:
        info = mc.self_info
        status(
            f"Connected: {describe_node(mc)}, auto-add contacts: "
            f"{'off' if info.get('manual_add_contacts') else 'on'}"
        )

        await check_firmware(mc)
        stored = await fetch_contacts(mc)
        status(f"Contacts synced: {'?' if stored is None else len(stored)}")

        gating = args.pair is not None or args.accept or args.ask
        if gating and not info.get("manual_add_contacts"):
            status(
                "! auto-add is ON on this node: the firmware adds contacts by "
                "itself, so --pair/--accept/--ask cannot gate anything. Turn "
                '"auto add contacts" off in the MeshCore client first.'
            )

        if args.import_card and not await import_card(mc, args.import_card):
            return 1

        channels: Optional[dict[int, str]] = None
        if args.new_channel or args.channels:
            if args.new_channel and await create_channel(mc, args.new_channel) is None:
                return 1
            channels = {
                idx: info.get("channel_name", "")
                for idx, info in (await channel_table(mc)).items()
                if not channel_is_empty(info)
            }
            listed = ", ".join(f"{i}:{n}" for i, n in channels.items()) or "none"
            status(f"Channels: {listed}")

        listener = DirectMessageListener(
            mc,
            show_raw=args.raw,
            show_adverts=args.show_adverts,
            accept_all=args.accept_all,
            accept_patterns=args.accept,
            ask=args.ask,
            channels=channels,
            pair=args.pair is not None,
            pair_key=args.pair or None,
            pair_timeout=args.pair_timeout,
            reply_text=None if args.no_reply else args.reply_text,
        )
        listener.subscribe()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, listener.stop)

        if args.advert:
            # Companion nodes do not advertise on a timer: other nodes only
            # learn about this one when it sends an advert. Zero-hop reaches
            # direct neighbours, flood gets repeated across the mesh.
            res = await mc.commands.send_advert(flood=args.advert == "flood")
            if res is None or res.type == EventType.ERROR:
                status(f"Advert ({args.advert}) failed: {res.payload if res else 'no reply'}")
            else:
                status(f"Advert sent ({args.advert}).")
        if args.pair is not None:
            listener.open_pairing_window()

        if args.no_reply:
            status("Waiting for direct messages (not replying) — Ctrl+C to quit.")
        else:
            status(
                f"Waiting for direct messages, replying {args.reply_text!r} "
                "— Ctrl+C to quit."
            )

        await listener.run()
    finally:
        await mc.disconnect()

    return 1 if listener.lost_connection else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print MeshCore direct messages received by a USB-attached node."
    )
    add_connection_args(parser)
    parser.add_argument(
        "--advert",
        choices=("zero-hop", "flood"),
        help="send an advert on startup so other nodes can discover this one "
        "(zero-hop = direct neighbours only, flood = repeated across the mesh)",
    )
    parser.add_argument(
        "--show-adverts",
        action="store_true",
        help="also report adverts this node hears from others",
    )
    parser.add_argument(
        "--pair",
        nargs="?",
        const="",
        metavar="KEYPREFIX",
        help="open a one-shot pairing window: add a single contact, either the "
        "one whose public key starts with KEYPREFIX or one confirmed in the "
        "terminal; nothing else is added",
    )
    parser.add_argument(
        "--pair-timeout",
        type=float,
        default=PAIR_TIMEOUT,
        metavar="SECONDS",
        help="how long the pairing window stays open (default: %(default)s)",
    )
    parser.add_argument(
        "--import-card",
        metavar="CARD",
        help="add a contact from a meshcore://… card instead of waiting for an "
        "advert",
    )
    parser.add_argument(
        "--accept",
        action="append",
        default=[],
        metavar="PATTERN",
        help="add a pending contact only when its name matches this glob or its "
        "public key starts with this hex prefix (repeatable)",
    )
    parser.add_argument(
        "--ask",
        action="store_true",
        help="ask in the terminal before adding each pending contact",
    )
    parser.add_argument(
        "--accept-all",
        action="store_true",
        help="add every node that advertises — convenient for bring-up, but it "
        "lets anything within earshot into the contact list",
    )
    parser.add_argument(
        "--channels",
        action="store_true",
        help="also print channel (group) messages, not just DMs",
    )
    parser.add_argument(
        "--new-channel",
        metavar="NAME",
        help="create a private channel with a fresh random key in the first free "
        "slot and print the key to add on other devices (implies --channels)",
    )
    parser.add_argument(
        "--reply-text",
        default=DEFAULT_REPLY_TEXT,
        metavar="TEXT",
        help="what to send back for each direct message (default: %(default)s)",
    )
    parser.add_argument(
        "--no-reply",
        action="store_true",
        help="only listen; do not answer incoming direct messages",
    )
    parser.add_argument(
        "--raw", action="store_true", help="also print the raw payload of each message"
    )
    args = parser.parse_args()
    configure_logging(args.debug)

    if args.list_ports:
        print(describe_ports())
        return 0

    if args.pair:
        key = args.pair.lower()
        if any(c not in "0123456789abcdef" for c in key) or len(key) < 4:
            status("--pair takes a public key prefix in hex, at least 4 characters.")
            return 2
        if len(key) < 12:
            status(f"Note: '{key}' is a short prefix; 12+ characters identify a node.")
        args.pair = key

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
