# HopTalk Relay Protocol, version 1 ("HT1")

Status: specification, version 1.0 (2026-09-25). This document is the contract between the HopTalk iOS app and
HopTalk Relay (the server). It is normative for both sides. When the server's behaviour and this document disagree,
the server has a bug.

Audience: the developer of a HopTalk client. Nothing here requires knowing how the server is built; server
behaviour is described only where a client depends on it.

Conventions:

- MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are used as in RFC 2119.
- "Client" means the HopTalk app together with the user's own MeshCore companion node (the "device", a Wio Tracker L1
  Pro in the first release). "Server" means HopTalk Relay, which appears on the mesh as one MeshCore companion
  contact.
- "Byte" always means a byte of UTF-8. Every length limit in this document is in bytes unless it says "characters".
- In sequence charts, message ids are shortened: `…456` stands for `1790294400123456`.

## 1. Overview

HopTalk has no HTTP API. A client talks to the server only through MeshCore direct messages (DMs), and every DM
carries exactly one protocol message.

Before any DM can flow, the two nodes must be MeshCore contacts of each other:

1. The user adds the server to their node from the server's contact card (a `meshcore://…` link or its QR code, which
   the server's operator hands out).
2. The operator adds the user's node to the server, either from the node's contact card or through the server's pairing
   mode (the operator watches for the node's advert).

Until step 2 has happened the server cannot decrypt the client's DMs. Nothing comes back, not even an error. A client
SHOULD tell the user "the server has not added your device yet" when its first sign-in gets no answer for a few
minutes.

What the protocol does:

| Need | Client sends | Server answers |
|---|---|---|
| Create an account or sign in (one user may own several devices) | `A` | `a` or `e` |
| Check that a username exists, before starting a conversation | `Q` | `q` or `e` |
| Send a message (up to 10 parts) | `M`, one per part | `k` (which parts the server holds) or `e` |
| Receive a message | answers every `m` part with `K` | the server sends `m` parts until the device reports every part |
| Say that the user read a message | `R` | `r` or `e` |
| Learn that a sent message was delivered or read | answers every `s` with `C` | the server sends `s` until confirmed |
| Get messages that were missed while the device was off | `F <peer>` or `F *` | `f`, then the missed messages as `m` parts |

The server delivers every message to **every device** of the recipient, retries each device separately until that
device confirms, sends "delivered" and "read" receipts to **every device** of the sender, and never treats a MeshCore
firmware ACK as proof that the app received anything.

## 2. What MeshCore gives the protocol, and what it does not

- **Size.** A MeshCore DM carries at most 160 bytes of text. This protocol limits every DM to **150 bytes** so that it
  also fits older companion firmware and BLE links that cut frames a few bytes shorter.
- **C strings.** A `0x00` byte truncates a DM. The protocol never uses it.
- **No reliability.** DMs can be lost, duplicated and reordered. A lost firmware ACK makes MeshCore apps send the same
  text again (the firmware itself never retries), so duplicates are normal. Links can be asymmetric: A's DMs reach B
  while B's DMs (and B's firmware ACKs) do not reach A.
- **Firmware ACK.** A firmware ACK (`PUSH_CODE_SEND_CONFIRMED`) means that the other *node* decrypted the packet. It
  does not mean that the other *app* saw it (the phone may be disconnected from its node for hours), and it is itself
  lost independently of the DM. The protocol therefore never uses firmware ACKs for correctness; only protocol
  acknowledgements count.
- **Authentication.** MeshCore encrypts every DM with a secret shared by the two contacts. The server identifies a
  device by its public key, and the client identifies the server the same way (CO-1).
- **MeshCore timestamp.** Every DM carries a 32-bit timestamp chosen by the sending app. It is not part of the protocol,
  with one exception: the server treats two DMs from the same device with the **same MeshCore timestamp and the same
  text** as one DM (a firmware-level repeat) and ignores the second one. A client MUST therefore give every DM it sends
  a new, strictly increasing MeshCore timestamp (CO-15).
- **Server identity.** If the operator ever reconfigures the server's node, the server gets a new identity and a new
  contact card. Users then have to add the new card; the old contact stops answering.

## 3. Recognising protocol traffic

A DM is protocol traffic if and only if all of the following hold:

1. On the client: it comes from the pinned server contact (CO-1). On the server: it comes from a contact the server
   holds.
2. Its MeshCore `txt_type` is 0 (plain text).
3. Its text starts with `HT`, then 1–3 decimal digits (the protocol version), then one space: `^HT[0-9]{1,3} `.

A DM that fails rule 1 or 2 is ignored by the protocol. A DM that fails rule 3 is "other text": the server stores it in
its log and **never answers it**; the client MAY show such a DM from the server as a plain message but MUST NOT act on
it. A DM that passes rule 3 with a version other than `1` is handled as described in §11.

The server never answers acknowledgements (`K`, `C`), lower-case (server) types, errors, or other text. Two servers, or
a server and a chat bot, therefore cannot keep each other busy.

## 4. Syntax

### 4.1 Grammar (ABNF, RFC 5234)

The grammar is over Unicode scalar values encoded as UTF-8. String literals marked `%s` are case-sensitive.

```abnf
; ----- framing ------------------------------------------------------------------------------------
protocol-dm       = magic version SP body
body              = client-body / server-body    ; client-body only from a client, server-body only from the server
magic             = %s"HT"
version           = "1"                          ; this specification
SP                = %x20                         ; exactly one space; never two spaces, never a tab

; ----- client -> server ---------------------------------------------------------------------------
client-body              = account-request / query-request / message-part-request
                         / delivery-acknowledgement / read-request / receipt-acknowledgement
                         / refresh-request
account-request          = %s"A" SP username SP password
query-request            = %s"Q" SP username
message-part-request     = %s"M" SP username SP message-id SP part-number "/" part-count SP part-text
delivery-acknowledgement = %s"K" SP username SP message-id SP received-set
read-request             = %s"R" SP username SP message-id
receipt-acknowledgement  = %s"C" SP username SP message-id SP receipt-level
refresh-request          = %s"F" SP refresh-target

; ----- server -> client ---------------------------------------------------------------------------
server-body       = account-reply / query-reply / send-status-reply / delivery-part / read-reply
                  / receipt-push / refresh-reply / error-reply
account-reply     = %s"a" SP username
query-reply       = %s"q" SP username SP existence
send-status-reply = %s"k" SP username SP message-id SP received-set
delivery-part     = %s"m" SP username SP message-id SP part-number "/" part-count SP part-text
read-reply        = %s"r" SP username SP message-id
receipt-push      = %s"s" SP username SP message-id SP receipt-level
refresh-reply     = %s"f" SP refresh-target SP message-count
error-reply       = %s"e" SP error-code SP request-type [SP error-reference]

; ----- fields -------------------------------------------------------------------------------------
username          = 3*16( %x41-5A / %x61-7A / %x30-39 )      ; A-Z a-z 0-9, 3 .. 16: the rules of 5.1
refresh-target    = username / "*"                            ; "*" = every conversation
message-id        = nonzero-digit 0*15DIGIT                   ; 1 .. 9999999999999999
part-number       = nonzero-digit [ DIGIT ]                   ; shape only: range 1 .. 10 is a value rule (5.4)
part-count        = nonzero-digit [ DIGIT ]                   ; shape only: range 1 .. 10, number <= count (5.4)
received-set      = 1*10( "0" / "1" )                         ; exactly part-count characters
receipt-level     = %s"D" / %s"R"                             ; delivered / read
existence         = "1" / "0"
message-count     = "0" / ( nonzero-digit 0*3DIGIT )          ; 0 .. 9999
request-type      = %x41-5A / "?"                             ; the letter of the request being answered
error-code        = 1*16( %x41-5A / "_" )                     ; see section 10
error-reference   = ( username SP message-id ) / refresh-target / version-number
version-number    = 1*3DIGIT
password          = *tail-character                           ; shape only: the rules of 5.2 are value rules
part-text         = *tail-character                           ; shape only: the rules of 5.5 are value rules
tail-character    = %x01-D7FF / %xE000-10FFFF                 ; any Unicode scalar value except NUL
nonzero-digit     = %x31-39
DIGIT             = %x30-39
```

### 4.2 Parsing rules the grammar alone does not say

- **Verbatim tails.** `password` and `part-text` are always the last field of their DM and run to the end of the text.
  A parser splits the header on single spaces exactly as many times as the type has header fields and takes the rest
  verbatim. A tail may start or end with spaces and may contain spaces. Nobody trims, normalises or re-encodes a
  `part-text`: the server forwards it byte for byte.
- **Strictness.** Anything that does not match the grammar exactly is a syntax error: two spaces, a tab between
  fields, a leading zero in a number, a trailing space after a non-tail field, an extra or missing field, a username
  that breaks §5.1 (usernames are fully described by the grammar, and clients validate them before sending, CO-14), a
  part number or count of three digits, a lower-case error code. The server answers a syntax error in a request with
  `e SYNTAX` (§10) and silently drops a malformed acknowledgement. The client silently drops a malformed server DM.
- **Value rules come second.** Only a request that matches the grammar is checked against the value rules, and each
  broken value rule has its own error code with the request's reference: the part rules of §5.4 and §5.5 (number and
  count 1–10, number ≤ count, text 1–104 bytes of allowed characters) → `PART_INVALID`; the password rules of §5.2 →
  `PASSWORD_INVALID`. So `HT1 M Bob 5 2/1 x`, `HT1 M Bob 5 1/11 x` and `HT1 M Bob 5 1/1 ` (empty text) are
  `PART_INVALID`, while `HT1 A ab hunter2222` (a two-letter username) is `SYNTAX`.
- **Unknown types.** The server answers an upper-case letter it does not know with `e UNSUPPORTED <letter>`. The client
  MUST ignore a lower-case type it does not know (forward compatibility, §11). The server ignores lower-case types
  from clients.
- **Error references.** `error-reference` alternatives overlap on purpose (a username and a version number can both be
  digits); the `request-type` in the same DM tells which one it is (§10.1).

## 5. Field rules

### 5.1 Usernames

- 3–16 characters from `A–Z`, `a–z`, `0–9`. Nothing else: no underscore, dot, space or non-ASCII letter.
- **Case-insensitive.** `Bob`, `bob` and `BOB` are the same user; only one of them can exist.
- **Canonical case.** The server remembers the spelling used when the account was created and always sends that
  spelling (in `a`, `q`, `m`, `k`, `r`, `s`, `f`). A client SHOULD display the canonical spelling and MUST compare
  usernames case-insensitively.
- A client may send any case in a request; `Q bob` finds `Bob`. A username that breaks these rules is a syntax error
  (§4.2); the client checks it and shows the rules itself (CO-14).

### 5.2 Passwords

- 8–64 characters after Unicode NFC normalisation, and at most 64 bytes of UTF-8 (so at most 32 Cyrillic letters).
  A character here is one Unicode scalar value (a code point), not a user-perceived character: a letter with a
  combining mark that NFC cannot compose counts as two, so `a` + U+0332 COMBINING LOW LINE, four times, is 8
  characters and valid.
  In Swift, count `unicodeScalars`, not `Character`s. The shared test vectors fix this reading.
- No control characters (U+0000–U+001F, U+007F–U+009F). No leading or trailing space (U+0020). Spaces inside are
  allowed.
- The client SHOULD send the password in NFC. The server normalises to NFC before hashing and before verifying, so the
  same password typed on two keyboards matches.
- A password that breaks these rules (including a control character) is answered `e PASSWORD_INVALID A <username>`.
- The password travels inside a MeshCore DM, which is encrypted between the two nodes. The server stores only a salted
  hash and never logs the password.

### 5.3 Message ids

- 1–16 decimal digits, no leading zero (1 … 9 999 999 999 999 999).
- **Unique per sender user**, across all of that user's devices, for all time.
- RECOMMENDED value: the sending device's clock in microseconds since the Unix epoch, forced to increase on that device
  (`id = max(now_in_microseconds, last_id + 1)`) and persisted before its first use. A 2026 timestamp has 16 digits.
- A message is identified by (sender user, id). Incoming and outgoing messages of one conversation can share an id,
  because ids are unique per sender only: a client identifies a message by (direction, peer, id).

### 5.4 Parts and received-sets

- `part-number` and `part-count`: 1–10, `part-number ≤ part-count` (otherwise `e PART_INVALID`).
- `received-set`: exactly `part-count` characters. Character *i* (1-based, left to right) is `1` when part *i* has been
  received, otherwise `0`. A single-part message has the set `1`. All ones means the whole message is held.

### 5.5 Part text

- Non-empty; at most **104 bytes** of UTF-8 (§7); Unicode scalar values only.
- No control characters except tab (U+0009) and line feed (U+000A). Clients MUST convert CR LF and CR to LF before
  splitting.
- A part is always cut at a code point boundary (§8.1). Parts are concatenated without separators.
- Allowed characters: tab, line feed, U+0020–U+007E and every scalar value from U+00A0 on. A part text that breaks
  any rule of this section is answered `e PART_INVALID`.

### 5.6 Receipt levels

- `D`: the message was delivered to at least one device of the recipient.
- `R`: the message was read on at least one device of the recipient. `R` implies `D`.

## 6. Messages

Every DM is `HT1 <type> <fields>`. Upper-case types go from client to server, lower-case types from server to client.

### 6.1 Client → server

| Type | Name | Grammar | Server answers with | Example | Bytes | Worst case |
|---|---|---|---|---|---|---|
| `A` | Register or sign in | `A <username> <password>` | `a` or `e` | `HT1 A ivan correct horse battery` | 32 | 87 |
| `Q` | Does this user exist? | `Q <username>` | `q` or `e` | `HT1 Q bob` | 9 | 22 |
| `M` | One part of a message | `M <recipient> <id> <n>/<c> <text>` | `k` or `e` | `HT1 M Bob 1790294400123456 1/1 Привет, Боб!` | 52 | 150 |
| `K` | Delivery acknowledgement | `K <sender> <id> <received-set>` | nothing | `HT1 K ivan 1790294400123456 1` | 29 | 50 |
| `R` | I have read this message | `R <sender> <id>` | `r` or `e` | `HT1 R ivan 1790294400123456` | 27 | 39 |
| `C` | Receipt acknowledgement | `C <recipient> <id> <D\|R>` | nothing | `HT1 C Bob 1790294400123456 D` | 28 | 41 |
| `F` | Refresh missed messages | `F <peer>` or `F *` | `f` or `e` | `HT1 F ivan` / `HT1 F *` | 10 / 7 | 22 |

**Requests** (`A Q M R F`) are retried by the client until an answer arrives (CO-3). **Acknowledgements** (`K C`) are
never retried and never answered (CO-4).

#### `A`: register or sign in

`HT1 A <username> <password>` → `HT1 a <canonical>`

The device identity is the MeshCore contact the DM came from. The server:

1. Answers `e PASSWORD_INVALID A <username>` if the password breaks §5.2.
2. If nobody has this username (case-insensitively): creates the account with this password, links the device to it,
   and answers `a <canonical>`.
3. Otherwise, if this device has sent 5 wrong passwords in its current window (15 minutes from its first wrong
   password), answers `e RATE_LIMITED A <username>` without checking the password until the window ends; the count
   then starts again from 0.
4. Otherwise verifies the password:
   - wrong: `e WRONG_PASSWORD A <username>`, and the device's count of wrong passwords grows by one (the first one
     starts the window);
   - right: clears that count and answers `a <canonical>`, after linking the device if it is not linked, changing
     nothing if it is already linked to this user, or relinking it (§6.3) if it is linked to **another** user.

A retry after a lost `a` finds the account and the link already in place and gets `a` again, except while the device
is rate-limited: step 3 comes first, so even a retry of a sign-in that succeeded gets `e RATE_LIMITED` until the window
ends (§10.2). A retry after a lost `e WRONG_PASSWORD` is checked again and counts as another wrong password. `a` does
not say whether the account was created or already existed: in both cases the device is now signed in as
`<canonical>`.

#### `Q`: does this user exist?

`HT1 Q <username>` → `HT1 q <canonical> 1` when the user exists, `HT1 q <username-as-sent> 0` when not. The device must
be signed in (otherwise `e NOT_SIGNED_IN Q <username>`). Asking about one's own username answers `1`.

#### `M`: one part of a message

`HT1 M <recipient> <id> <n>/<c> <text>`

- Every part of one message carries the same recipient, id and count; parts may be sent and may arrive in any order.
- The server answers with a `k` carrying its received-set for that message (§8.3). All ones means the server has
  stored the whole message and will deliver it; this is the "sent" confirmation.
- The recipient must exist (`e NO_SUCH_USER M <recipient> <id>`) and must not be the sender (`e SELF M …`). The device
  must be signed in (`e NOT_SIGNED_IN M …`).
- A message to a user who currently has no devices is accepted and kept; it is delivered when a device of that user
  asks for it (`F *`, §8.5).
- Repeating a part with exactly the same text is harmless: the server stores nothing new and answers again. The same
  id with a different recipient, part count or part text is `e ID_CONFLICT M <recipient> <id>`, whichever of the
  user's devices sends it. While a message is incomplete, a part the server does not hold yet is accepted only from
  the device that sent the message's first part; from another device it is `ID_CONFLICT` too (§8.4).

#### `K`: delivery acknowledgement

`HT1 K <sender> <id> <received-set>`

Sent by a receiving device for **every** `m` part it receives, including duplicates and parts of a message it already
has completely, carrying the device's full received-set for that message (§8.3). All ones means "this device has the
whole message": the server stops sending it to this device. `K` is never answered.

#### `R`: read

`HT1 R <sender> <id>` → `HT1 r <sender> <id>`.

Sent once the user has seen a complete incoming message; retried until `r`. The server records the read on the
message and on this device's delivery, and starts `R` receipts to the sender's devices. An `R` also counts as a
delivery confirmation for that device. If this user never received a message (sender, id): `e NOT_FOUND R <sender>
<id>`.

#### `C`: receipt acknowledgement

`HT1 C <recipient> <id> <D|R>`

Sent by a device for **every** `s` it receives, including duplicates and receipts for message ids it does not know
(another device of the same user sent that message). Never answered.

#### `F`: refresh missed messages

`HT1 F <peer>` → `HT1 f <peer> <count>`; `HT1 F *` → `HT1 f * <count>`.

- `F <peer>`: sent whenever the user opens the conversation with `peer`. The server re-delivers to **this device
  only**, oldest first and strictly one message at a time, every message from `peer` to this user that this device has
  not confirmed, with fresh retry counters (§8.5). It also restarts the receipts that this device has not confirmed yet
  for messages **this device** sent to `peer` (receipts about messages sent from another device of the user carry ids
  this device does not know, so they are not restarted).
- `F *`: the same for every peer at once, one independent refresh per peer. It does not restart receipts. Sent after
  every successful sign-in (CO-11), because a newly linked device does not know which conversations have missed
  messages.
- `count` is the number of messages that will follow as `m` parts (at most 9999 is shown); `0` means nothing is
  missing. For a refresh that is already running, it is the number still outstanding.
- §8.5 says exactly what a refresh covers and how a repeated `F` restarts a running one. Messages that another device
  of the user already received are not copied to a new device (there is no history sync in version 1).

### 6.2 Server → client

| Type | Name | Grammar | Client does | Example | Bytes | Worst case |
|---|---|---|---|---|---|---|
| `a` | Signed in | `a <username>` | completes the pending `A` for that username (compared case-insensitively); stores the canonical spelling; an `a` without a pending `A`: §9.2 | `HT1 a ivan` | 10 | 22 |
| `q` | User exists result | `q <username> <1\|0>` | completes the pending `Q` for that username | `HT1 q Bob 1` | 11 | 24 |
| `k` | Send status | `k <recipient> <id> <received-set>` | replaces the message's confirmed set (§8.3 rule 2); all ones = "sent"; zeros = resend those parts | `HT1 k Bob 1790294400123456 1` | 28 | 50 |
| `m` | Delivery part | `m <sender> <id> <n>/<c> <text>` | stores the part, answers `K` | `HT1 m ivan 1790294400123456 1/1 Привет, Боб!` | 53 | 150 |
| `r` | Read accepted | `r <sender> <id>` | completes the pending `R` | `HT1 r ivan 1790294400123456` | 27 | 39 |
| `s` | Receipt | `s <recipient> <id> <D\|R>` | raises the message's status (never lowers it); answers `C` | `HT1 s Bob 1790294400123456 R` | 28 | 41 |
| `f` | Refresh accepted | `f <peer\|*> <count>` | completes the pending `F` | `HT1 f ivan 3` / `HT1 f * 5` | 12 / 9 | 27 |
| `e` | Error | `e <code> <request-type> [<reference>]` | completes the matching pending request with that error (§10) | `HT1 e NO_SUCH_USER M carol 1790294400123457` | 43 | 58 |

`m` carries the **sender's** canonical username where `M` carried the recipient's, and exactly the bytes the sender's
client sent for that part. `s` and `k` carry the **recipient's** canonical username.

### 6.3 Accounts and devices

- One user may own several devices. Messages go to all of them; receipts go to all of them.
- A device belongs to at most one user. Signing in on a device as another user (correct password) relinks it: from
  then on the device gets only the new user's traffic. The server cancels every pending delivery and receipt of the
  old user to that device. If the device later signs in as the old user again, a refresh (`F`) delivers what is still
  missing.
- No `M`, `R`, `F`, `m` or `s` says which account it belongs to. The client therefore sets the old account's pending
  requests aside before it sends `A` for another username, and clears its conversations once the switch is done
  (CO-13). A DM of the old account that is already on its way when the switch happens can still be taken as the new
  account's. A delayed retry of an earlier `A` that reaches the server after the switch moves the device back to that
  account, and the server answers `a <that account>` (§9.2 says what the client does).
- A device that is a server contact but has not signed in may send `A` only. `Q M R F` get `e NOT_SIGNED_IN`; `K` and
  `C` are ignored.
- Users are created only through `A`. Only the operator deletes users and devices. Deleting a user also deletes every
  message they sent or received, delivered or not; deleting a device ends every delivery and receipt to it.

## 7. Byte budget

Constants: the DM limit is **150 bytes**; `HT1` is 3 bytes; the type letter is 1; each separating space is 1; the
longest username is 16; the longest id is 16; the longest part field (`10/10`) is 5; the longest error code
(`PASSWORD_INVALID`) is 16.

**Part text: at most 104 bytes, always.** `M` and `m` have the same layout, `HT1 X <username> <id> <n>/<c> <text>`. In
the worst case (16-character username, 16-digit id, `10/10`) the header is 46 bytes, and 46 + 104 = 150. Because the
limit is a constant, a part the client sends as `M <recipient> …` always fits when the server forwards the same bytes as
`m <sender> …`, whatever the two usernames are. The server rejects a longer part with `e PART_INVALID`.

```
"HT1 M KonstantinIvanov 9999999999999999 10/10 " = 3+1+1+1+16+1+16+1+5+1 = 46 bytes      46 + 104 = 150 ✓
"HT1 m AlexandraPetrova 9999999999999999 10/10 "                          = 46 bytes      46 + 104 = 150 ✓
```

Capacity: 104 bytes per part is 104 ASCII characters, 52 Cyrillic letters or 26 four-byte emoji. A message has at most
10 parts, so at most 1 040 bytes (fewer when a grapheme cluster cannot be split cheaply, §8.1).

Every other type stays far below 150 bytes. The "Worst case" columns of §6.1 and §6.2 are 6 bytes (`HT1 `, the letter,
a space) plus the longest value of every field and the spaces between them: username 16, id 16, received-set 10,
password 64, message count 4, error code 16. The longest `e` is `HT1 e <code16> M <user16> <id16>`, 58 bytes.

## 8. Multi-part messages

### 8.1 Splitting (done only by the sending client)

The client splits the text; the server stores the parts and forwards each one unchanged, so it never splits text
itself. Any split is valid when every part is non-empty, is valid UTF-8 cut at a code point boundary, is at most 104
bytes, and there are at most 10 parts. The reference algorithm keeps extended grapheme clusters (UAX #29; Swift's
`Character`) whole whenever that costs no extra part:

```
function split_message_text(text):
    require text is non-empty and contains only allowed text characters (5.5), with CR LF and CR already turned into LF
    budget = 104
    code_point_parts = pack_units_into_parts(code_points(text), budget)        # greedy: the fewest parts possible
    grapheme_parts   = pack_units_into_parts(grapheme_clusters(text), budget)
    parts = grapheme_parts if count(grapheme_parts) == count(code_point_parts) else code_point_parts
    if count(parts) > 10:
        fail "message too long" (tell the user how much to cut)
    return parts

function pack_units_into_parts(units, part_byte_budget):
    parts = []; current_part = ""
    for unit in units:
        if utf8_length(unit) > part_byte_budget:          # one grapheme cluster longer than a whole part
            for code_point in unit:
                if utf8_length(current_part + code_point) > part_byte_budget:
                    parts.append(current_part); current_part = ""
                current_part = current_part + code_point
            continue
        if utf8_length(current_part + unit) > part_byte_budget:
            parts.append(current_part); current_part = ""
        current_part = current_part + unit
    if current_part != "": parts.append(current_part)
    return parts
```

Properties: every part is non-empty; no code point is ever cut; greedy code-point packing gives the fewest parts, and
grapheme packing is used exactly when it gives the same number of parts, so a grapheme cluster is cut only when keeping
it whole would cost an extra part or when it alone is longer than 104 bytes.

The client MUST store the exact part texts of a message before sending its first part, and every resend of part *n*
MUST be byte-identical. The server rejects a different text for a part it already holds with `e ID_CONFLICT`.

### 8.2 Header of every part

`M <recipient> <id> <n>/<c> <text>` from the client, `m <sender> <id> <n>/<c> <text>` from the server. Every part repeats
the username, the id and the count, so any part can start reassembly on the receiving side and parts can arrive in any
order.

### 8.3 Sending, acknowledging and resending parts (the same rules in both directions)

The sender ("S": the client for `M`, the server for `m`) and the receiver ("R": the server for `M`, the client for `m`):

1. **Rounds.** In one round S sends every part R has not reported as received, in ascending part order (the first round
   sends all parts). A retry of a request is a new round.
2. **Status after every part.** R answers every part it receives, including duplicates and parts of a message it
   already has completely, with a status DM (`k` from the server, `K` from the client) that carries R's **complete**
   received-set for that message; R stores a part before it reports it. S takes the **latest** status it received as
   R's current set and replaces its earlier view with it, never an OR of several statuses: R may have lost parts it
   reported before (a reinstalled app, an upload the server deleted, §8.4), and the next round then resends them. A
   lost or duplicated status costs nothing; a reordered older one costs only duplicate parts, which R answers again.
3. **Coalescing.** R sends an *incomplete* status only once 5 s have passed without a new part of the same message, so
   one status covers a burst of parts. R sends a *complete* (all ones) status at once. The server always coalesces like
   this; clients SHOULD.
4. **Missing parts.** When a status with zeros arrives after S has sent its whole round, S sends the missing parts soon:
   the client at once; the server as its next round, within about 5 s. A status with zeros that arrives while S is
   still sending a round only updates S's view.
5. **No status.** When no status arrives before S's retry timer runs out, S starts the next round, which again sends
   every part R has not reported.
6. **Completion.** A status whose own set is all ones ends the transfer: for `M` it is the server's acceptance (the
   message is "sent"); for `m` it is this device's delivery confirmation. A later status for a completed transfer
   changes nothing.

### 8.4 Incomplete messages

- **Server as receiver.** The server keeps the parts of an incomplete message per (sender user, id) and deletes them
  24 hours after the **last** part arrived. Nothing is ever delivered from an incomplete message; the recipient never
  sees a fragment. If the client resumes later, even after the server deleted the parts, the server's statuses show
  which parts it lacks (rule 2 of §8.3), and the next rounds resend them. While the message is incomplete, only the
  device that sent its first part may add parts: another device of the same user may repeat a part the server holds,
  and anything else from it is `e ID_CONFLICT`, so two messages that share an id by mistake are never spliced.
- **Client as receiver.** The client SHOULD keep incomplete deliveries for at least 7 days and MUST NOT show an
  incomplete message as a normal message. If it drops one, a later retry or refresh from the server resends every part
  its status marks missing.

### 8.5 Refresh (missed messages)

- A refresh (`F <peer>`) makes the server re-deliver to the requesting device, **oldest first** (in the order the server
  accepted them), every message from `peer` to this user that this device has not confirmed (whether the server gave
  up on it or is still retrying it), plus every message from `peer` that **no** device of this user ever received. Each
  message gets a fresh set of attempts (the server's normal retry strategy, §13.2).
- **One message at a time.** The next message is sent only after the device confirmed the previous one with an
  all-ones `K` (or `R`).
- **Stop on failure.** If a message still cannot be delivered when its attempts run out, the server stops: the later
  messages of that refresh are not sent, so the device never has a gap. The device needs a new `F` (for example the
  next time the user opens the conversation).
- **A new `F` while the refresh runs**, a retry of the same `F` included, restarts the message being sent with fresh
  attempts, at once, and keeps the rest of the refresh in order. Opening the conversation again is therefore enough to
  get a slow refresh moving; the client retries `F` only while the conversation is open (CO-10), or a message the
  device cannot receive would never be given up.
- **At most 3 messages in flight per device.** The server sends at most 3 messages at a time to one device, new ones
  and refreshes together. A message that has not been sent yet, the first message of a new refresh included, waits
  until one of them is confirmed or given up, which can take up to the longest retry pause (§13.2) while those 3 wait
  between attempts. A message restarted by a new `F` keeps its place.
- Messages from `peer` that arrive while the refresh is running join its end, so the conversation stays in order on
  this device.
- The device still receives other conversations' messages normally while a refresh runs.
- `F *` starts one such refresh per peer. Refreshes of different peers run side by side; each stays in order.

## 9. Acknowledgements, idempotency and deduplication

### 9.1 What the server does with repeats

| Incoming | Identity | First arrival | Same content again | Different content |
|---|---|---|---|---|
| Any DM | (device, MeshCore timestamp, text), within 24 h | processed | **ignored, no answer** (a firmware-level repeat) | processed normally |
| `A` | (device, username) | processed | checked again: a right password finds the device linked, so `a` again and nothing changes (`RATE_LIMITED` while the device is rate-limited); a wrong password counts again | another username is another sign-in |
| `Q` | — | answered | answered again | — |
| `M` part | (sender user, id, part number) | stored | nothing stored, same `k` again | `e ID_CONFLICT` for another recipient, count, or part text, and for a part not held yet from another device while the message is incomplete |
| `K` | (message, device) | set recorded | no change | the latest set replaces the recorded one; a set of the wrong length is ignored |
| `R` | (message, device) | read recorded | `r` again, no change | — |
| `C` | (message, device) | level recorded | no change | a lower level than already recorded is ignored; `R` for a receipt sent only as `D` confirms `D` |
| `F` | (device, peer) while a refresh runs | refresh started | outstanding count again; the refresh keeps its order, and its current message starts again with fresh counters | — |

The server also does not send the **same reply text** to the same device twice within 10 s (it would only duplicate a
reply that is already on its way). Client retries are at least 20 s apart (CO-3), so this never hides the answer to a
retry, but it does hold back the answer to a *new* request that equals an answer sent less than 10 s earlier (a second
wrong password for the same username, reopening a conversation, the same `Q` again): that answer comes with the
request's retry. Because a retried `A` counts as another wrong password, a client SHOULD wait 10 s after
`e WRONG_PASSWORD` before it sends a new `A` for the same username. The one exception to the rule: when a reply sent
over a stored route gets no firmware ACK, the server resets that route and sends the same reply once more by flood,
however soon that is.

### 9.2 What the client does with repeats

- An `m` part is identified by (peer, id, part number): keep the first copy, answer every copy with `K` carrying the
  full set, and show the message once, when it is complete.
- An `s` sets the status of the outgoing message (peer, id) to max(status, level), `R` implying `D`, and is always
  answered with `C`, even for an unknown id. A `k` replaces the confirmed set of the outgoing message (peer, id)
  (§8.3 rule 2); after an all-ones `k` the message is "sent" and later `k`s change nothing.
- An `a`, `q`, `r`, `f` or `e` completes the matching pending request (§6.2, §10.1). One that matches no pending
  request is ignored, except an `a`, which always reports the account the device is linked to at that moment:
  - an `a` for the username of the latest `A`, which an `e` already completed (a delayed answer to an earlier copy):
    the device is signed in as that user;
  - an `a` for another account than the app's, with no `A` for it pending: a delayed copy of an earlier `A` has moved
    the device to that account (§6.3). The app MUST treat itself as signed out (CO-13), tell the user and sign in
    again. A delayed `a` DM can trigger this too; signing in again is then harmless.

## 10. Errors

### 10.1 Format

`HT1 e <code> <request-type> [<reference>]`. `request-type` is the letter of the request being answered, or `?` when
the request's letter could not be determined. The reference repeats the request's correlation fields, so the client
can find the pending request, and is present only when those fields were syntactically valid:

| Request | Reference |
|---|---|
| `A`, `Q` | `<username>` as sent |
| `M`, `R` | `<peer> <id>` as sent |
| `F` | `<peer>` or `*` |
| a request with a version other than 1 | type `?`, reference `<received-version>` |
| an unknown upper-case letter X | type `X`, no reference |

### 10.2 Codes

| Code | Meaning | Possible for | Permanent? | Client action |
|---|---|---|---|---|
| `SYNTAX` | The request does not match the grammar (§4), including a username that breaks §5.1 | any | yes | Stop retrying; this is a client bug; log it |
| `VERSION` | This server does not speak that protocol version | any | yes | Stop; tell the user the server or the app needs updating |
| `UNSUPPORTED` | Unknown request letter | any | yes | Stop; the server is older than the app |
| `NOT_SIGNED_IN` | The device is not linked to any user | `Q M R F` | until a successful `A` | Stop; send the user to sign-in |
| `PASSWORD_INVALID` | The password breaks §5.2 | `A` | yes | Show the password rules |
| `WRONG_PASSWORD` | The username exists and the password is wrong | `A` | yes | Ask for the password again; send the new `A` no sooner than 10 s after this error (§9.1) |
| `RATE_LIMITED` | 5 wrong passwords from this device in its 15-minute window, which starts at its first wrong password | `A` | until the window ends | Tell the user to wait up to 15 minutes; keep the `A` pending and send it again when 15 minutes have passed (a sign-in that had already succeeded, its `a` lost, then gets `a`) |
| `NO_SUCH_USER` | The peer does not exist | `M F` | yes | Show "user not found"; for `M`, mark the message failed |
| `SELF` | The peer is the sender's own account | `M F` | yes | Stop |
| `ID_CONFLICT` | This id is already used by a different message of the same sender | `M` | yes | Send the message again under a **new** id, with parts computed for it |
| `PART_INVALID` | A part that matches the grammar but breaks §5.4 or §5.5: number or count outside 1–10, number above count, empty text, a forbidden character, or text longer than 104 bytes | `M` | yes | Stop; client bug; log it |
| `NOT_FOUND` | This user has no incoming message (peer, id) | `R` | yes | Stop |

A client MUST treat an error code it does not know as permanent for that request and log it. Acknowledgements (`K`,
`C`) never produce errors: an invalid or unknown acknowledgement is ignored.

## 11. Versioning and extensibility

- The digits after `HT` are the protocol version; this document defines version 1, the only one the server speaks
  today. An incompatible change (a new field, a different part budget, different semantics) becomes version 2, and a
  server that supports several versions answers each client in the version the client used.
- A request in another version (an upper-case letter after `HT<version> `) is answered with `HT1 e VERSION ? <version>`.
  Anything else in another version is ignored, which keeps two servers of different versions from answering each
  other.
- Within version 1, new message types MAY be added as new letters. A client MUST ignore lower-case types it does not
  know. The server answers upper-case types it does not know with `e UNSUPPORTED <letter>`, which tells a newer client
  that this server is older.
- Neither side may append optional fields to an existing type in version 1: strict parsing rejects them.

## 12. Client obligations (MUST unless stated otherwise)

- **CO-1 Pin the server.** Take the server's public key from its contact card; treat only DMs from that contact as
  protocol traffic. If the operator issues a new card (a reconfigured server), replace the pinned key.
- **CO-2 One protocol message per DM**, at most 150 bytes of UTF-8, no `0x00`, `txt_type` 0.
- **CO-3 Retry every request** (`A Q M R F`; `F` only as CO-10 and CO-11 say) until its answer or a permanent error
  arrives. The first retry comes no sooner than 20 s after the previous send was handed to the device's node;
  RECOMMENDED schedule 20, 40, 80, 160, 300 s, then every 300 s while the app runs. Resume pending requests when the
  app starts. Every retry is a new MeshCore send with a new timestamp (CO-15).
- **CO-4 Never retry an acknowledgement** (`K`, `C`). Send one for every `m` and every `s` received, including
  duplicates, except those CO-13 tells the client to discard.
- **CO-5 Message ids** as in §5.3: unique per user for all time, increasing per device, persisted before first use,
  never reused for different content. On `e ID_CONFLICT`, send the message again under a new id.
- **CO-6 Sending parts.** Split with §8.1; persist the exact part texts before the first send; resend byte-identical
  parts; take each `k` as the server's current set, replacing the previous one (§8.3 rule 2); resend exactly the parts
  it marks missing (at once); treat an all-ones `k` as "sent". Without any `k`, a retry round resends every part not
  yet confirmed. CO-17 gives the exact timer rules.
- **CO-7 Receiving parts.** Reassemble by (peer, id); display a message only when complete, and only once; store every
  part before answering it, and answer every part with `K` carrying the full set. An incomplete `K` SHOULD be
  coalesced as in §8.3 rule 3 (sent once 5 s have passed without a new part of that message); a complete `K` MUST go
  out at once.
- **CO-8 Read.** After the user has seen a complete incoming message, send `R` once and retry it until `r`.
- **CO-9 Receipts.** Apply `s` monotonically (`R` implies `D`) and answer each one with `C`, even for unknown ids.
- **CO-10 Refresh a conversation.** Send `F <peer>` whenever the user opens the conversation with `peer`, and retry it
  (CO-3) only while that conversation stays open: every `F` that reaches the server restarts the refresh's current
  message (§8.5). Keep at most one outstanding `F` per peer. After `f <peer> <n>`, expect up to `n` messages as `m`
  parts.
- **CO-11 Refresh everything after sign-in.** Send `F *` at once after every successful `A`, including after a
  reinstall and after an account switch, and retry it at most three times (20, 40, 80 s): a lost `f` costs only the
  count, because the missed messages arrive as `m` parts anyway. SHOULD also send `F *` when the app reconnects to its
  node after more than 30 minutes without any DM from the server.
- **CO-12 Ordering and deduplication.** Order a conversation by message id (the senders' clocks) and deduplicate by
  (direction, peer, id); never by arrival order or by the MeshCore timestamp.
- **CO-13 Account switch.** Before sending `A` for a username other than the current one, stop sending every pending
  request of the current account and set them aside: a request that reaches the server after the switch is executed as
  the new account. If that `A` fails (an `e`), nothing was switched: send them again. After the `a` for the new
  username, discard them, clear the local conversations and send `F *` at once (CO-11). Switching back later brings only
  messages the device never confirmed, so the app SHOULD warn before a switch that the current account's unsent messages
  and its history on this device will be lost. An `a` for an account the app did not ask for: §9.2. A client with no
  signed-in account (for example a fresh install on a tracker that is still linked to someone) discards `m` and `s`
  without answering them; the server retries, and after sign-in `F *` brings what is still missing. So does a client
  whose `A` for another username is still pending: until the answer arrives it cannot tell which account an `m` or `s`
  belongs to, and a `K` or `C` for it could confirm a message of the new account that the switch then clears.
- **CO-14 Usernames.** Validate locally with §5.1 before sending; compare case-insensitively; display the canonical
  case the server returns.
- **CO-15 Pacing on the device's own node.** The companion node tracks only 8 expected firmware ACKs and has a
  16-packet buffer shared by sending and receiving. The client MUST keep at most 4 DMs waiting for a firmware ACK on
  its node and leave at least 2 s between two DMs; a DM stops waiting when its ACK arrives or clamp(1.2 × the node's
  suggested timeout, 3 s, 60 s) after `MSG_SENT` (the server's own rule). It MUST give every DM a new, strictly
  increasing MeshCore timestamp (`max(now_seconds, previous + 1)`) and SHOULD NOT use MeshCore's firmware-level retry
  (same timestamp, higher `attempt`): its own retries (CO-3) replace it, and the server ignores such repeats. Error 3
  (`ERR_CODE_TABLE_FULL`) for a DM of at most 150 bytes means the buffer is full: wait a few seconds and send the same
  DM again with a new timestamp; this is not a retry of the request.
- **CO-16 Route hygiene on the device's own node.** MeshCore never falls back to flooding by itself when a stored route
  stops working. The client MUST therefore: (a) reset its node's route to the server (`CMD_RESET_PATH`) before answering
  a server DM that arrived by flood (`path_len` ≠ 0xFF), unless its node reported a new route to the server
  (`PUSH_CODE_PATH_UPDATED`) in the last 30 s; (b) reset that route when a DM to the server that its node sent direct
  (`MSG_SENT` type 0) gets no firmware ACK within clamp(1.2 × the node's suggested timeout, 3 s, 60 s) (CO-15), so the
  next DM floods. It SHOULD skip (b) when the server has already answered that DM (the matching `k`, `a`, `q`, `r`, `f`
  or `e` arrived): the route works and only the ACK was lost.
- **CO-17 Sending a message, step by step.** For one pending `M` message:
  1. A *round* sends, in part order and paced by CO-15, every part not yet confirmed. The retry timer starts when the
     last part of the round has been handed to the node, with the current schedule step (20, 40, 80, 160, 300 s, then
     300 s, CO-3).
  2. A `k` with zeros is progress, not an answer. It replaces the confirmed set. If the round has been sent completely,
     send the parts it marks missing at once (§8.3 rule 4) and restart the timer after the last of them at the same
     schedule step; this resend is not a retry round.
  3. When the timer runs out with no `k` received since it started, advance the schedule step and start the next
     round.
  4. An all-ones `k` completes the message ("sent"), and so does any `s` for it (the server has delivered it, so it
     holds every part). A permanent `e` completes it as failed.
  Other requests (`A Q R F`) have one DM per round and complete on their answer or a permanent `e`.

## 13. What the client can rely on

### 13.1 Guarantees

- **SG-1** An answer is sent only after the state it reports has been stored: an all-ones `k` means the message is
  durably stored and will be delivered, unless the operator deletes the sender or the recipient (§6.3).
- **SG-2** Every request is idempotent: repeating it gives the same answer and no second effect, with two deliberate
  exceptions. A repeated `A` is checked again, so a repeated wrong password counts again toward `RATE_LIMITED`, and
  while the device is rate-limited even a repeat of a successful `A` gets `RATE_LIMITED`. `F` for a refresh that is
  running, a retry included, restarts its current message's attempts (§8.5).
- **SG-3** The server never answers `K`, `C`, lower-case types, errors or other text.
- **SG-4** Every `m` part fits 150 bytes and carries exactly the bytes the sender's client sent for that part.
- **SG-5** A message is sent to every device the recipient had when the message was accepted, each device retried
  separately per the server's retry strategy until it answers with an all-ones `K` (or `R`) or the attempts run out.
  After that, an `F` from that device starts again (§8.5). Deleting the sender, the recipient or that device (§6.3)
  ends it.
- **SG-6** During a refresh, a peer's messages arrive in acceptance order, one message at a time; the next one is sent
  only after the previous one was confirmed; the refresh stops rather than skip a message.
- **SG-7** Receipts only move forward: `D` is never sent after `R` has been confirmed by that device.
- **SG-8** Firmware ACKs are never taken as delivery. A device that received a message but whose `K` keeps getting
  lost keeps receiving copies of it until one `K` gets through (each copy is answered with `K` again).

### 13.2 Server timing (defaults; the operator can change the retry values and the receipt hold-back)

| What | Default |
|---|---|
| Attempts per message, per device (and per receipt) | 6 |
| Pause after attempt *n* | 30 s × 2^(n−1), at most 600 s: 30, 60, 120, 240, 480, 600 s |
| A device that never answers: last attempt, then give up | attempts at about 0, 0.5, 1.5, 3.5, 7.5, 15.5 minutes; failed after about 25.5 minutes |
| Rounds after the first | every part the device has not confirmed; a `K` with zeros after a complete round brings the next round forward to about 5 s |
| Messages in flight to one device | 3 at a time, new ones and refreshes together (§8.5) |
| "Delivered" receipt held back, so that a quick read sends only the "read" receipt | 15 s |
| Incomplete `k` coalescing | sent once 5 s have passed without a new part of that message |
| Incomplete message kept | 24 hours after its last part |
| Same reply text to the same device not repeated within | 10 s |
| Wrong passwords before `RATE_LIMITED` | 5 per device in a 15-minute window that starts at its first wrong password; then `RATE_LIMITED` until the window ends, and the count starts again; a retry of the same wrong password counts again |

## 14. Message sequence charts

Legend: `──▶` arrives, `──✗` is lost, `⋯` time passes. Users: `ivan` (devices D1, D2) and `Bob` (devices B1, B2).
Every line is one DM; the `HT1 ` prefix is left out.

### 14.1 Register and sign in

```
Device D1 (new, not linked)                  Server
 │ A ivan "correct horse battery" ──────────▶│ no user "ivan": creates it, links D1
 │ ✗◀───────────────────────────── a ivan ───│ lost
 │ ⋯ 20 s without an answer: retry, same text, new MeshCore timestamp
 │ A ivan "correct horse battery" ──────────▶│ "ivan" exists, password right, D1 already linked: nothing changes
 │ ◀────────────────────────────── a ivan ───│ signed in as "ivan"
 │ F * ─────────────────────────────────────▶│
 │ ◀─────────────────────────────── f * 0 ───│ nothing missed

Device D2 (ivan's second tracker, not linked)
 │ A IVAN "correct horse batterx" ──────────▶│ wrong password: D2 has 1 failure
 │ ✗◀────────────── e WRONG_PASSWORD A IVAN ─│ lost
 │ ⋯ 20 s: retry
 │ A IVAN "correct horse batterx" ──────────▶│ checked again: D2 has 2 failures
 │ ◀─────────────── e WRONG_PASSWORD A IVAN ─│
 │ A IVAN "correct horse battery" ──────────▶│ right: links D2 to ivan, D2's failures cleared
 │ ◀────────────────────────────── a ivan ───│ canonical spelling "ivan"
 │ F * ─────────────────────────────────────▶│
 │ ◀─────────────────────────────── f * 0 ───│
```

### 14.2 Message upload with lost acknowledgements and a lost part

```
Device D1 (ivan)                             Server
 │ M Bob …456 1/1 "Привет, Боб!" ───────────▶│ stores the message; starts delivery to Bob's devices
 │ ✗◀──────────────────── k Bob …456 1 ──────│ lost
 │ ⋯ 20 s: retry, byte-identical part
 │ M Bob …456 1/1 "Привет, Боб!" ───────────▶│ duplicate: nothing new stored
 │ ◀───────────────────── k Bob …456 1 ──────│ status: sent

 │ M Bob …457 1/3 <104 bytes> ──────────────▶│ holds {1}; waits for more parts
 │ M Bob …457 2/3 <104 bytes> ──────✗        │
 │ M Bob …457 3/3 <40 bytes> ───────────────▶│ holds {1,3}
 │                                           │ 5 s without a new part of …457: status
 │ ◀─────────────────── k Bob …457 101 ──────│
 │ part 2 is missing: resend it at once
 │ M Bob …457 2/3 <104 bytes> ──────────────▶│ complete: stored, delivery starts
 │ ◀─────────────────── k Bob …457 111 ──────│ a complete status goes out without waiting; status: sent

If "k … 101" is lost too, D1's retry timer (20 s) runs out and it resends every part not yet confirmed (1, 2, 3):
part 1 → still incomplete; part 2 → complete → "k … 111"; part 3 → duplicate of a stored message → "k … 111"
(not sent twice within 10 s).
```

### 14.3 Fan-out to a recipient with two devices, one of them off

```
Server                                       Bob's B1 (on)                  Bob's B2 (switched off)
 attempt 1 to each device
 m ivan …456 1/1 "Привет, Боб!" ────────────▶ B1
 m ivan …456 1/1 "Привет, Боб!" ──────────────────────────────────────────────✗ B2
 ◀─────────────────────── K ivan …456 1 ──── B1
 B1 is done and never gets this message again; the message is now "delivered"
 +30 s   attempt 2 to B2 only ✗      (the server's node also resets a stale route to B2, so it floods)
 +90 s   attempt 3 ✗   +210 s attempt 4 ✗   +450 s attempt 5 ✗   +930 s attempt 6 ✗
 +1530 s B2 is given up for now; ivan's view stays "delivered" (B1 has it)
 later: B2 is switched on, Bob opens the chat with ivan → "F ivan" (14.5)

Meanwhile, 15 s after B1's K (the "delivered" hold-back), both of ivan's devices get "s Bob …456 D" and answer
"C Bob …456 D" (14.4); D2 answers although it did not send …456 and does not know the id.
```

### 14.4 Receipts

```
Bob's B1                       Server                                      ivan's D1        ivan's D2
 │                             │ B1 confirmed …456 → "delivered", held back 15 s
 │                             │ s Bob …456 D ─────────────────────────────▶ D1   shows "delivered"
 │                             │ ✗◀──────────────────────── C Bob …456 D ─── D1   lost
 │                             │ s Bob …456 D ─────────────────────────────────────────────▶ D2
 │                             │ ◀────────────────────────────────────────── C Bob …456 D ── D2 (did not send …456)
 │                             │                                                          then D2 is switched off
 │ R ivan …456 ───────────────▶│ records the read on the message and on B1; the pending D is not sent again
 │ ✗◀───────── r ivan …456 ────│ lost
 │ ⋯ 20 s
 │ R ivan …456 ───────────────▶│ already recorded: no change
 │ ◀────────── r ivan …456 ────│
 │                             │ s Bob …456 R ─────────────────────────────▶ D1   shows "read"
 │                             │ ◀───────────────────────── C Bob …456 R ─── D1   done for D1
 │                             │ a delayed copy of "C Bob …456 D" ─▶ ignored: the recorded level stays R
 │                             │ s Bob …456 R ─────────────────────────────────────────────✗ D2
 │                             │ retried per the strategy, all lost: given up for D2
 │                             │ (D2 did not send …456, so a later "F Bob" from D2 does not restart it: §6.1)

Had Bob read the message within the 15 s hold-back, D1 would have received only "s Bob …456 R".
```

### 14.5 Refresh of missed messages, with the stop-on-failure rule

```
Before: messages …456, …457 (3 parts) and …458 from ivan to Bob; B2 was off and missed all three.
Bob's B2                                     Server
 │ F ivan ──────────────────────────────────▶│ three messages to re-deliver to B2, oldest first
 │ ◀────────────────────────────── f ivan 3 ─│
 │ ◀─ m ivan …456 1/1 "Привет, Боб!" ────────│ message 1, attempt 1
 │ K ivan …456 1 ───────────────────────────▶│ message 1 done → message 2
 │ ◀─ m ivan …457 1/3, 2/3, 3/3 ─────────────│ message 2, attempt 1
 │ K ivan …457 111 ──✗                       │
 │                                           │ +30 s attempt 2: every part B2 has not confirmed
 │ ◀─ m ivan …457 1/3 ───────────────────────│
 │ K ivan …457 111 ─────────────────────────▶│ B2 already had all three parts → message 2 done (the rest of
 │                                           │ the round is not sent) → message 3
 │ (B2 goes out of range)                    │ ivan sends …459 meanwhile: it joins the end of this refresh
 │                               ✗◀──────────│ m ivan …458 1/1: attempts 1 … 6, all lost
 │                                           │ message 3 given up: the refresh STOPS; …459 is not sent (no gap)
 │ ⋯ back in range; Bob opens the chat again │
 │ F ivan ──────────────────────────────────▶│ a new refresh with fresh attempts: …458, then …459
 │ ◀────────────────────────────── f ivan 2 ─│

Had Bob opened the chat again while the refresh was still trying …458 (say after its fifth attempt), "F ivan" would
have answered "f ivan 2" and sent …458 again at once with six fresh attempts, …459 still waiting behind it.
```

## Appendix A. Testing by hand from the stock MeshCore app

The protocol is plain text, so two phones with the stock MeshCore app can exercise the server by typing DMs.

1. In the server's admin panel, add both test nodes as contacts (by card or by pairing). On each phone, add the server
   from the card or QR code on the panel's Contacts page.
2. Phone A: `HT1 A alice hunter2222` → `HT1 a alice`. Phone B: `HT1 A bob hunter2222` → `HT1 a bob`.
3. A: `HT1 F *` → `HT1 f * 0`.
4. A: `HT1 Q bob` → `HT1 q bob 1`.
5. A: `HT1 M bob 1 1/1 Hello Bob` → `HT1 k bob 1 1`.
6. B receives `HT1 m alice 1 1/1 Hello Bob` and types `HT1 K alice 1 1`. Until B does, the server repeats the `m`
   (30 s, 60 s, 120 s … later).
7. About 15 s after B's `K`, A receives `HT1 s bob 1 D` and types `HT1 C bob 1 D`.
8. B: `HT1 R alice 1` → `HT1 r alice 1`. A receives `HT1 s bob 1 R` and types `HT1 C bob 1 R`.
9. Two parts: A types `HT1 M bob 2 1/2 Hello ` and, a little later, `HT1 M bob 2 2/2 again`. The server answers
   `HT1 k bob 2 10` about 5 s after the first part (typing takes longer than that) and `HT1 k bob 2 11` at once after
   the second. B receives both parts and types `HT1 K alice 2 11`.
10. B: `HT1 F alice` → `HT1 f alice 0`.

Tips:

- Use a new id for every message. Turn off auto-capitalisation and "smart punctuation" (it turns a double space into
  ". "): the strict parser rejects both changes.
- The stock app's own automatic resend uses the same MeshCore timestamp, so the server ignores it; if nothing comes
  back, type the DM again. The server does not repeat an answer it sent less than 10 s earlier (§9.1), so wait 10 s
  before typing a request whose answer would be the same (the same `Q`, another wrong password).
- Every DM in both directions appears on the admin panel's Messages → Traffic page, with its decoded meaning.

## Appendix B. Test vectors

The server repository publishes machine-readable vectors in `src/tests/protocol/vectors/`:
`splitting.json` (text → parts), `formatting.json` (message fields → exact DM text and byte length) and
`parsing.json` (DM text → parsed fields or the expected error). A client SHOULD run them in its own test suite. A few
examples:

| Input | Expected |
|---|---|
| 250 × `a` | 3 parts: 104, 104, 42 bytes |
| 60 × `Ж` (120 bytes) | 2 parts: 52 letters (104 bytes), 8 letters (16 bytes) |
| 5 × 👨‍👩‍👧‍👦 (each 7 code points, 25 bytes; 125 bytes in all) | grapheme packing: 100 + 25 bytes; code-point packing: 104 + 21 bytes; both need 2 parts, so the grapheme split is used |
| `HT1 M Bob 1790294400123456 1/1 Привет, Боб!` | valid; 52 bytes; recipient `Bob`, id 1790294400123456, part 1 of 1, text `Привет, Боб!` |
| `HT1 M Bob 01 1/1 x` | `SYNTAX` (leading zero) |
| `HT1 M Bob 5 2/1 x` | `PART_INVALID` (part number above part count) |
| `HT1 M Bob 5 1/11 x` | `PART_INVALID` (part count above 10) |
| `HT1 M Bob 5 1/1 ` (the text is empty) | `PART_INVALID` (empty text) |
| `HT1 M Bob 5 1/100 x` | `SYNTAX` (three-digit part count) |
| `HT1 A ab hunter2222` | `SYNTAX` (a username needs 3–16 characters) |
| `HT1 A bob short` | `HT1 e PASSWORD_INVALID A bob` |
| `HT1  Q bob` | `HT1 e SYNTAX ?` (two spaces: no letter after `HT1 `) |
| `HT1 K ivan 5 11` for a 3-part message | ignored (set length ≠ part count) |
| `HT2 Q bob` | `HT1 e VERSION ? 2` |
| `HT1 X foo` | `HT1 e UNSUPPORTED X` |
| `hello` | not protocol traffic; no answer |
