# hoptalk-relay

Reads direct messages from a MeshCore node attached over USB and prints them to
the terminal. Tested against a Seeed XIAO nRF52840 + Wio-SX1262 running the
MeshCore **USB/serial companion** firmware, on macOS.

Two scripts:

* `listen_dm.py` — read incoming DMs, pair the nodes you want to talk to.
* `node_setup.py` — keep the node's contact handling locked down: no auto-add,
  print the node's contact card, wipe contacts on request.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

No hardware needed: `tests/fake_node.py` stands in for a USB-attached node. It
implements the interface `SerialConnection` offers MeshCore and answers the
companion-protocol commands these scripts send, so the real reader, dispatcher
and command handlers run unchanged — what the tests assert on is the bytes that
reach the node and the lines the scripts print. Replies come back from a
background task after a short delay, like a real serial link: an instant
in-process answer would hide the ordering the scripts have to cope with.

One test (`test_fetch_presets_from_the_live_api`) talks to MeshCore's preset API
and skips itself when there is no network.

## Run

```bash
.venv/bin/python listen_dm.py
```

The port is auto-detected (`/dev/cu.usbmodem*` and friends). Override it with
`--port /dev/cu.usbmodem1101` or `MESHCORE_PORT=...`; `--list-ports` shows
everything the system currently exposes.

Output looks like:

```
[17:43:38] Alice: hello from the mesh   (direct, SNR -7.5, sent 17:43:36)
```

Messages go to stdout, status lines to stderr, so `listen_dm.py > dm.log` keeps
the log clean.

## Replying

Every plain direct message is answered with `RECEIVED`, and the reply line says
whether the node saw an ack:

```
[17:43:38] Alice: hello from the mesh   (direct, SNR -7.5, sent 17:43:36)
-> Alice: RECEIVED (direct, acked)
```

`--reply-text 'ok'` changes the wording, `--no-reply` only listens. What is never
answered:

* a message whose text is exactly the reply text — otherwise two nodes running
  this would answer each other forever;
* CLI-type messages (`txt_type` 1), which are a remote-administration channel,
  not a conversation;
* channel messages, which carry no sender key to reply to.

That echo guard covers the symmetric case only. Two relays with *different* reply
texts would still ping-pong, so do not point two of these at each other.

### Stale routes

The firmware sends a DM along the contact's stored `out_path` whenever it has
one, and never falls back to flood by itself — and its own acks go the same way
(`BaseChatMesh::sendMessage`, `sendAckTo`). A route learned while the other node
was nearby therefore keeps swallowing every reply once it moves out of range, and
nothing refreshes it: the node only learns a new route when the other side
returns one, which it only does after receiving something by flood. The stale
route prevents exactly the traffic that would fix it.

So the script clears the route itself, in two places:

* a message that arrived **by flood** means the sender had no working route here,
  so the stored route back to them is cleared before replying;
* a reply that goes **unacked over a stored route** clears it and floods one
  retry.

Clearing it (`RESET_PATH`, the "Reset path" button in the clients) also unsticks
the firmware's own acks, because they read the same field. The log shows what
happened:

```
[15:14:33] KoalaBean: wow   (flood, 2 hops, SNR 1.75, sent 15:14:32)
~ cleared the stored path to KoalaBean (it reached us by flood); the next send floods
-> KoalaBean: RECEIVED (flood, acked)
```

### Repeated messages

Every attempt of one message carries the sender's original timestamp — the
attempt number is what makes each packet unique — so sender, timestamp and text
together identify one message however many times it arrives. Retries are printed
and marked `retry`, but answered only once: a sender repeating itself means the
replies are not getting through, and more of them down the same route would only
add airtime.

Other flags: `--raw` also dumps each message's parsed payload, `--debug` turns on
the `meshcore` protocol log.

## Making this node discoverable

Companion nodes do not advertise on a timer — other nodes only learn about this
one when it sends an advert. If a device does not see this node in its Discover
list, send one:

```bash
.venv/bin/python listen_dm.py --advert flood --show-adverts
```

`--advert zero-hop` is heard only by nodes in direct radio range; `--advert
flood` is rebroadcast by repeaters across the mesh. The advert has to be sent
while the other device is powered on and listening — it is a one-shot broadcast,
not something the other side can poll for.

`--show-adverts` reports adverts this node hears from others, which tells you
whether the RF link works in that direction at all.

## Pairing a node for DMs

A DM is encrypted with an ECDH shared secret derived from the sender's key, so
this node can only decrypt (and ACK) a DM from a node whose public key it already
holds. An advert is the only packet that carries a public key, so pairing means:
get one advert from the node you want, add that one, add nothing else.

First turn **auto add contacts off** in the MeshCore client for this node —
otherwise the firmware adds contacts by itself and none of the gates below can do
anything. The script warns at startup when it is still on.

Then pick a gate. In order of how much they actually verify:

```bash
# 1. Key first: read the pager's public key in its settings, pair only that key
.venv/bin/python listen_dm.py --pair 99887766554433

# 2. Confirm a fingerprint: the terminal prints the full key, compare it with
#    the one on the other device's screen and answer y
.venv/bin/python listen_dm.py --pair

# 3. No radio involved: paste the card the other device shares (QR / URI)
.venv/bin/python listen_dm.py --import-card 'meshcore://0102ab…'
```

`--pair` is a one-shot window (120 s, `--pair-timeout` to change): it adds exactly
one contact and then closes, so a node that advertises a second later is not
added. Press *Advert* on the other device while the window is open — zero-hop and
standing next to it, so the advert does not travel further than it has to.

Pairing is mutual: the other device needs this node's key too. Add `--advert
zero-hop` to send ours in the same run.

For long-running use there are looser gates — `--accept PATTERN` (repeatable;
name glob or public-key hex prefix), `--ask` (confirm each one), and
`--accept-all` (everything within earshot, bring-up only). Trust the key prefix
over the name: a name is whatever the other node claims.

Without any of these flags the script never changes the contact list.

## Channels

A channel is symmetric: everyone holding the 16-byte channel key can read and
post, and no contacts are involved. Messages from a channel therefore arrive
without any key exchange — but they also carry **no sender public key**, only
whatever name the sending client wrote into the text. You cannot build a contact
from a channel message, and a name in one is not authenticated.

```bash
.venv/bin/python listen_dm.py --new-channel hoptalk   # create + print the key
.venv/bin/python listen_dm.py --channels              # print channel messages too
```

`--new-channel` generates a random key, writes it to the first free channel slot
and prints it as hex and base64 to enter on the other device. It never overwrites
a slot that is already in use.

## node_setup.py

```bash
.venv/bin/python node_setup.py                  # report, turn auto-add off, print the card
.venv/bin/python node_setup.py --check          # report only; exit 1 if auto-add is on
.venv/bin/python node_setup.py --wipe-contacts  # delete every stored contact
.venv/bin/python node_setup.py --reset          # factory reset the node
.venv/bin/python node_setup.py --setup          # first-run setup after a reset
.venv/bin/python node_setup.py --discover-path KoalaBean   # measure a route
```

### --discover-path

Measures the route to one contact and reports both directions:

```
Path discovery to Platypuff (fed2902733ba), up to 25s:
  the node currently stores: a 0-hop route
  us -> them : 0 hop(s)   no hops — a direct neighbour
  them -> us : 0 hop(s)   no hops — a direct neighbour
```

The firmware forces this request to flood — it clears `out_path` for the send
(`CMD_SEND_PATH_DISCOVERY_REQ` in `MyMesh.cpp`) — so an answer proves that our
packets reach that node **and** that its reply finds its way back. Silence means
one of the two legs failed, without saying which; compare runs from different
distances to narrow it down. Raise `--discover-timeout` for a distant contact:
the firmware would suggest about five seconds, and an answer that takes twenty is
still an answer.

It only measures. The firmware reports the paths to the app and deliberately does
not store them ("DON'T send reciprocal path!"), so this diagnoses routing without
changing it.

### --setup

Walks through a fresh node in the order the web client does, then falls through
to the auto-add step below:

1. **Name** — prompted, current name offered as the default.
2. **Radio settings** — pick from the preset list. Those presets are not on the
   node: the MeshCore clients fetch them from `api.meshcore.nz/api/v1/config`
   (`suggested_radio_settings`), and so does this script. The preset the node is
   already on is marked in the list, and entry `0` lets you type frequency /
   bandwidth / SF / CR by hand, which is also what you get when the list cannot
   be fetched.
3. **Default path hash size** — 1, 2 or 3 bytes. Some presets carry the size
   their region uses (the `2B` / `3B` in a preset description) and that becomes
   the default. The node stores a *mode*, one less than the size.
4. **Direct message acks** — set to 2, no question asked.
5. **Contact auto-add** — turned off by the usual step.

`--name`, `--preset` and `--path-hash-size` pre-answer steps 1–3, so the whole
run can go without prompts. A preset is matched by title, case-insensitively,
and a unique substring is enough (`--preset 'australia (narrow)'`).

### Reboot

`--setup` reboots the node at the end and reconnects to report what came back;
`--no-reboot` skips that, and `--reboot` does it on its own or alongside another
action. **No setting needs it** — the firmware applies each one immediately
(`radio_driver.setParams` for the radio) and calls `savePrefs()`, and it reads
`path_hash_mode` and `multi_acks` fresh every time it uses them. The reboot is a
check that the settings really persisted, not a step that makes them take effect.

The reboot frame is `0x13` followed by the literal `reboot`, which the firmware
requires; it answers nothing and just reboots (after flushing pending contacts).
The script then waits for the USB serial port to drop and come back — up to 5 s
for it to disappear, 30 s for it to return, and a second more for the CDC
endpoint. If it returns under a different name, that one is used and the change
is reported.

Two separate firmware settings decide whether an advert becomes a contact by
itself (`examples/companion_radio/MyMesh.cpp`):

* `manual_add_contacts` — when its low bit is 0, **every** advert is auto-added
  and the per-type bits are not even consulted.
* `autoadd_config` — consulted only in manual mode; bits `0x02`/`0x04`/`0x08`/
  `0x10` still auto-add chat / repeater / room server / sensor nodes. Bit `0x01`
  means something else (overwrite the oldest contact when the table is full), so
  the script leaves it alone, and `autoadd_max_hops` only ever narrows auto-add.

The script reports both, and turns off the manual flag and the four type bits if
any of them is on. The node persists both settings, so this is a one-off.

`--wipe-contacts` lists what it is about to delete and waits for you to type
`delete` (`--yes` skips that, for scripted use). Afterwards it asks the node how
many contacts are left rather than trusting the local cache. Deleting a contact
means DMs from that node can no longer be decrypted until it is paired again.

`--reset` formats the node's filesystem, which is not reversible. It prints what
will be lost and waits for you to type the node's **name** — that also proves you
are wiping the board you think you are, when more than one is plugged in. What a
reset actually does, from the firmware:

* the stored identity is gone, so `begin()` generates a **new random key** — every
  other device still points at the old one and has to be paired again;
* the name goes back to the firmware default, and the radio parameters go back to
  the build's defaults, which may not match your other nodes;
* contacts, channels and all settings are erased;
* `manual_add_contacts` and `autoadd_config` are zero again, i.e. **auto-add is
  back on** — re-run `node_setup.py` once the node reboots.

The firmware disables its serial interface *before* formatting, so the OK frame
usually never arrives; the script treats a missing reply as success and only fails
on an explicit error code from the node. It sends `0x33` followed by the literal
`reset`, which is what the firmware matches on — `meshcore`'s own
`confirm_factory_reset()` sends a bare `0x33` and that check rejects it.

The contact card is a freshly signed self-advert, so the printed URI differs from
run to run even though it describes the same node. `--qr` renders it as a QR
block (needs `pip install qrcode`), `--no-card` leaves it out.

## Firmware version

Both scripts query the node at startup and print what it runs:

```
Firmware: v1.17.1-d929643 (Seeed Xiao-nrf52, build 14-Aug-2026, companion protocol 13)
```

Protocol 13 / firmware v1.17.1 is what everything here was checked against
(`TESTED_PROTOCOL` in `mcnode.py`). The check never blocks, it only warns:

* **older protocol** — names the features the node will reject: direct message
  acks need 7, default path hash size needs 10;
* **newer protocol** — says to re-check the two things these scripts take
  straight from the firmware source, the factory-reset payload and the auto-add
  config bits, because a change there fails *quietly*;
* **no answer to the device query** — says the version is unknown.

`--reset` repeats the warning right before its confirmation prompt when the
protocol is not 13: the firmware only acts on `0x33` + `reset`, and a firmware
that wanted a different payload would ignore the command — which looks exactly
like the successful case, since a real reset never answers either.

## Notes

- Only one program can hold the serial port: close the MeshCore web/desktop
  client (or `mccli`) before running this.
- On startup the script drains whatever is already queued on the node, then
  waits for the node's "messages waiting" push (with a 30 s poll as a fallback).
- Channel (group) messages are pulled off the node's queue but not printed —
  this is DM-only by design.
- The sender name comes from the contact list; an unknown sender shows as
  `<pubkey_prefix>`.
- Route info per message: `direct` means the packet arrived over a direct
  route, `flood, N hops` means it was flooded through N hops.
- The startup line reports the node's radio params (freq/SF/BW/CR) — they have to
  match on every node that should hear each other.
