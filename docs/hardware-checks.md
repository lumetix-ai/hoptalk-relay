# Hardware checks

The automated suite runs the real meshcore library against a fake companion firmware and a simulated mesh
(`src/tests/worker/fake_node`). It proves the server's logic, not the radio, the firmware or the USB link. This
checklist covers what only real hardware can show. Go through it before every release, and again after a firmware
update on the relay node or on the trackers.

Each check lists the steps and what you should see. When something differs, write down what you saw, with the time,
and keep the relay's log from that moment (`make --directory=src relay-logs`): the numbers you record are what the
defaults will be tuned with.

## What only hardware can answer

| Question | Check |
| --- | --- |
| Does the node reconnect by itself after it re-enumerates: through socat on macOS (where the TinyUSB firmware only talks while DTR is raised), and through the udev symlink on Linux? | 1, 11 |
| Does the factory reset command work on this firmware, and does the node come back with a new identity? | 2, 13 |
| Does the firmware export the node's private key, and does a reset node take it back, so that a reconfiguration keeps the relay's identity for users who never re-add its card? | 2, 13 |
| Does the XIAO, which has no clock that survives a reboot, get its clock back from the server? | 2, 11 |
| Which contact card forms does the stock MeshCore app import? | 3 |
| Are pairing adverts heard, and is a heard advert enough to add a node? | 4 |
| Does the protocol work end to end over real radios, typed by hand? | 5, 6 |
| Do Cyrillic, emoji and 10-part messages survive the trip byte for byte? | 6 |
| Are messages kept for a device that is off, and delivered by a refresh? | 7 |
| What does a tracker do with DMs while its phone is away? | 8 |
| Are stale routes repaired in both directions, and how does the 30-second path update window behave? | 9 |
| Does an asymmetric link cause a storm of route resets? | 10 |
| Does traffic survive a node reboot, a USB unplug, a worker restart and a power loss? | 11 |
| Do the packet pool, the pacing and the default timeouts fit this mesh and its duty cycle? | 12 |

## Equipment

- **The relay node**: a Seeed XIAO nRF52840 with a Wio-SX1262, MeshCore USB serial companion firmware v1.17.1
  (companion protocol 13), plugged into the server.
- **Tracker A and tracker B**: two Seeed Wio Tracker L1 Pro with MeshCore companion firmware, each connected over
  Bluetooth to a phone with the stock MeshCore app: **phone A** and **phone B**.
- **A repeater** for checks 9 and 10: a MeshCore repeater that the relay node and the trackers can all reach, and a
  place where a tracker hears the repeater but not the relay node.
- **The server**: the Linux deployment for the release run. Check 1 also has a macOS part, for the development bridge.

All nodes must use the same radio settings: the preset chosen in check 2.

## Before you start

1. Write down, for the results table at the end: the date, the commit (`git rev-parse --short HEAD`), whether the
   server is the Linux deployment or the Mac, the trackers' firmware versions, the MeshCore app's version and the
   phones' systems.
2. If an earlier run left users `alice` and `bob`, delete both on the Users page. That also deletes their devices and
   their messages, so the message ids below are free again, and checks 3 and 4 add the trackers afresh. On both phones,
   delete the relay's old contact.
3. Keep two views open during the whole session: the panel's Messages → *Traffic* page with *Live* on, which shows every
   DM in both directions with its decoded meaning, route, firmware ACK and route reset; and a terminal with
   `make --directory=src relay-logs`.
4. On both phones, turn off auto-capitalisation and smart punctuation: the parser is strict, and a capital letter or a
   double space turned into ". " changes the request.
5. How to type the requests: every DM starts with `HT1 `, as written below. Use a new message id for every new message.
   The stock app resends a DM that got no firmware ACK with the same MeshCore timestamp, and the server ignores such
   repeats; if nothing comes back, type the DM again. The server does not repeat an answer it sent less than 10 seconds
   earlier, so wait 10 seconds before typing a request whose answer would be the same.

Expected answers are written as the DM the phone receives, such as `HT1 a alice`.

## 1. USB connection and re-enumeration

**On the Linux server:**

1. Confirm the USB identifiers the udev rule relies on:
   `udevadm info --query=property --name=/dev/ttyACM0 | grep --extended-regexp 'ID_VENDOR_ID|ID_MODEL_ID|ID_SERIAL'`.
   Expected: `ID_VENDOR_ID=2886` and `ID_MODEL_ID=8044`. If they differ, correct
   `/etc/udev/rules.d/99-meshcore-node.rules` and the README, which still marks these values as unconfirmed.
2. `ls -l /dev/meshcore-node` points at the `ttyACM` device, and
   `udevadm info --query=property --name=/dev/meshcore-node | grep ID_MM_DEVICE_IGNORE` prints `ID_MM_DEVICE_IGNORE=1`.
3. Start the stack. Expected: the relay log reports the connection, and the dashboard's *Relay* card shows the
   connection *connected* over `serial /dev/meshcore-node`, with no failed attempts. The mode is *Not configured*
   before the first setup; check 2 shows the firmware and protocol the node reported.
4. Unplug the node, wait 30 seconds, plug it in again. Expected: within a few seconds the banner *The node is not
   connected*; after the replug, `/dev/meshcore-node` exists again (possibly pointing at another `ttyACM` number), and
   the worker reconnects within 30 seconds without any container restart.
5. Node → *Reboot node…*. Expected: the node disappears and comes back within about a minute and a half, and the worker
   reconnects by itself.

**On the Mac (development):**

1. `make --directory=src serial-bridge` in its own terminal. Expected: `Serving /dev/cu.usbmodem… on 127.0.0.1:5055.`,
   then the worker's connection, and the dashboard's *Relay* card *connected* over `tcp host.docker.internal:5055`.
2. If the worker connects to the bridge but the node never answers (command timeouts in the relay log), suspect the
   DTR line: the firmware only sends while the host holds DTR raised. Record it; the bridge would then need to set DTR
   explicitly.
3. Unplug and replug the node, then reboot it from the dashboard. Expected each time: the bridge logs
   `Connection closed`, then `Waiting for a device…` or at once `Serving …` again, possibly under a new
   `/dev/cu.usbmodem…` name, and the worker reconnects by itself within 30 seconds.

## 2. Setup and the identity change

1. Open the panel; the start page opens the setup wizard. *Start setup*.
2. Expected on the first step: the node's name, public key, firmware v1.17.1, model, protocol 13, radio, path hash size,
   multi-acks, automatic adding, contact count, clock and channel 0. **Write down the public key.**
3. *Factory reset…*, type the node's name, *Factory reset*. Expected, live: the reset is sent, the node disappears
   within 15 seconds and comes back within 90 seconds, and the wizard shows a new public key.
   - Expected: the new key differs from the one written down.
   - *The node ignored the reset* means the firmware did not accept the reset command. Stop here: the release is
     blocked until the reset payload is checked against this firmware.
   - *The node did not come back after the reset*: unplug it, plug it in again, press *Retry*, and record that it
     happened. It means the filesystem format failed.
4. Configure: name `HopTalk Relay`, the radio preset of your region, its path hash size, the maximum transmit power,
   and *Replace the Public channel* left off. *Review*, *Apply and reboot*.
5. Expected: 14 of the 16 steps finish with a check mark, including the reboot, the read-back and *Back up the new
   identity*; *Restore the relay's identity* (*The node keeps the new identity from the reset.*) and *Replace the Public
   channel* (*The Public channel is kept.*) show as skipped. The last page shows the contact card as a QR code and a
   `meshcore://…` link.
   - If *Back up the new identity* is skipped with *This node's firmware does not allow exporting its private key*,
     the firmware was built without `ENABLE_PRIVATE_KEY_EXPORT`. Record it: the first part of check 13 cannot pass
     with this firmware.
6. Expected on the dashboard: mode *Running*; the chosen name, radio and transmit power; multi-acks 2; firmware v1.17.1
   and protocol 13 with no *Unexpected firmware protocol version* banner; no drift; contacts on the node 0; a clock
   offset of a few seconds at most (the node lost its clock in the reboot, and the worker set it); *Identity backup*
   *Stored*, taken a moment ago.
7. Reload the page: the node is shown as configured, and the start page now opens the dashboard.

## 3. Contact cards

**The relay's card on the phones:**

1. Contacts page: the relay's card as a QR code and as a `meshcore://…` link.
2. Phone A: add the relay by scanning the QR code in the MeshCore app. Phone B: add it by pasting the link (AirDrop it
   or put it in a note).
3. Expected on both phones: a contact with the name from check 2, whose public key starts with the same hexadecimal
   digits as the key on the dashboard.
4. If the app refuses the card in either form, record exactly how. The panel offers only the signed card, the form the
   node exports; a different form would be a change to the panel.

**Tracker A's card on the relay:**

1. In phone A's app, copy tracker A's own contact card (its share or export action; the wording varies between app
   versions) and bring it to the panel.
2. Contacts → *Add by contact card*, paste it, *Check card*. Expected in the preview: tracker A's name, its full public
   key (compare it with the one the app shows), a chat node, the advert time, a valid signature and no problem.
3. *Add contact*. Expected: the row shows *being added*, then *on the node* within a few seconds; the dashboard counts
   one contact on the node.
4. Paste the same card again and check it. Expected: *This node is already a contact*. Paste the relay's own card.
   Expected: refused as the relay's own card. Change one hexadecimal digit in the middle of a copy of tracker A's card.
   Expected: refused, because the signature no longer matches.

## 4. Pairing

1. Contacts → *Pairing*: duration 120 seconds, an advert every 30 seconds, *Flood adverts* off. *Start pairing*.
   Expected: the countdown starts, one advert is sent at once and one more every 30 seconds.
2. Phone B: send a zero-hop advert from tracker B. Expected within seconds under *Heard nodes*: tracker B's name, its
   full public key (compare it with phone B's app), a chat node, and when it was first and last heard. The relay itself
   never appears. Other nodes nearby may appear too, and nothing is added by itself: the dashboard's contact count does
   not change.
3. *+* on tracker B. Expected: *Do you really want to add this contact?* with the name, key and type. *Yes, add*.
   Expected: *Added*, and tracker B in the contacts table, *being added* then *on the node*.
4. Expected on phone B: the relay's adverts are heard (the app updates the relay contact, or shows it as heard).
5. Let the countdown reach zero. Expected: the session ends by itself, and the list stays there with its *+* buttons for
   another 15 minutes.
6. Optional: repeat with *Flood adverts* on, from where tracker C (any third node) hears the relay only through a
   repeater. Record whether its advert reaches the list.

## 5. Sign-in and the hand-testing session

This is the session of [Appendix A](protocol.md#appendix-a-testing-by-hand-from-the-stock-meshcore-app) of the protocol,
with the sign-in cases around it and what the panel should show. Both trackers are contacts of the relay (checks 3 and
4), and both phones have the relay's contact.

| # | Phone | Types | Receives |
| --- | --- | --- | --- |
| 1 | A | `HT1 Q bob` | `HT1 e NOT_SIGNED_IN Q bob` |
| 2 | A | `HT1 A alice short` | `HT1 e PASSWORD_INVALID A alice` |
| 3 | A | `HT1 A alice hunter2222` | `HT1 a alice` |
| 4 | B | `HT1 A bob hunter2222` | `HT1 a bob` |
| 5 | B | `HT1 A alice wrongpassword` | `HT1 e WRONG_PASSWORD A alice` |
| 6 | A | `HT1 F *` | `HT1 f * 0` |
| 7 | A | `HT1 Q bob` | `HT1 q bob 1` |
| 8 | A | `HT1 Q carol` | `HT1 q carol 0` |
| 9 | A | `HT1 M bob 1 1/1 Hello Bob` | `HT1 k bob 1 1` |
| 10 | B | (nothing yet) | `HT1 m alice 1 1/1 Hello Bob`, repeated about 30, 90 and 210 seconds after the first while B does not answer |
| 11 | B | `HT1 K alice 1 1` | nothing; the repeats stop |
| 12 | A | (nothing) | `HT1 s bob 1 D`, about 15 seconds after B's `K` |
| 13 | A | `HT1 C bob 1 D` | nothing |
| 14 | B | `HT1 R alice 1` | `HT1 r alice 1` |
| 15 | A | (nothing) | `HT1 s bob 1 R` |
| 16 | A | `HT1 C bob 1 R` | nothing |
| 17 | A | `HT1 M bob 2 1/2 Hello ` (with the space at the end), and a little later `HT1 M bob 2 2/2 again` | `HT1 k bob 2 10` about 5 seconds after the first part, `HT1 k bob 2 11` at once after the second |
| 18 | B | (nothing yet) | `HT1 m alice 2 1/2 Hello ` and `HT1 m alice 2 2/2 again` |
| 19 | B | `HT1 K alice 2 11` | nothing |
| 20 | B | `HT1 F alice` | `HT1 f alice 0` |
| 21 | A | `hello` | nothing, ever |
| 22 | A | `HT1 X foo` | `HT1 e UNSUPPORTED X` |
| 23 | A | `HT2 Q bob` | `HT1 e VERSION ? 2` |

Expected in the panel:

- After row 4, the Users page lists `alice` and `bob`, each with one device (tracker A, tracker B). Row 5 changes
  nothing there: tracker B stays `bob`'s.
- After row 11, the Messages page shows message 1 from `alice` to `bob` with tracker B's delivery *delivered*; after row
  14 it is marked read. If B reads within 15 seconds of its `K`, A receives only `HT1 s bob 1 R`.
- On the Traffic page every row has a decoded meaning. Row 21 is classified as not protocol and has no reply. The
  sign-in rows read `HT1 A alice ********`: the password is never stored.
- `./scripts/docker-compose.sh logs app relay | grep --count hunter2222` prints `0`.
- When the stock app resends a DM by itself, the original Traffic row shows *× 1 firmware repeat*, and nothing more is
  answered.

## 6. Cyrillic, emoji, one part and ten parts

**One part:**

1. A: `HT1 M bob 3 1/1 Привет, Боб! 👋🏽 Ёжик 🇦🇺` → `HT1 k bob 3 1`.
2. Expected on B: `HT1 m alice 3 1/1 Привет, Боб! 👋🏽 Ёжик 🇦🇺`, with the text exactly as sent, including the skin tone
   and the flag. B: `HT1 K alice 3 1`.

**Ten parts.** The lines below are the parts the protocol's reference splitter makes of one 996-byte message; the
longest is 121 bytes as sent and 123 bytes as B receives it. Put them on phone A (AirDrop a text file, or a note) and
paste them one DM at a time, in this order, **leaving out part 5**:

```
HT1 M bob 4 1/10 Проверка HopTalk: десять частей, кириллица и эмодзи 👋🏽. Каж
HT1 M bob 4 2/10 дая часть не длиннее ста четырёх байт UTF-8 📡. Семью (
HT1 M bob 4 3/10 👨‍👩‍👧‍👦) и флаг (🇦🇺) нельзя резать посередине. Пот
HT1 M bob 4 4/10 ерянную часть клиент отправит ещё раз 🔁. Получатель под
HT1 M bob 4 5/10 тверждает части набором единиц и нулей 🧩. Сервер пересы
HT1 M bob 4 6/10 лает части без изменений ✅. Ёжик, щука, жёлтый шарф, чёрны
HT1 M bob 4 7/10 й кофе ☕. Съешь же ещё этих мягких французских булок, да в
HT1 M bob 4 8/10 ыпей чаю 🍵. В сети MeshCore сообщения идут через ретранслято
HT1 M bob 4 9/10 ры (🛰). Сервер ждёт ответа от каждого устройства 📬. Коне
HT1 M bob 4 10/10 ц проверки: сравните текст с исходным 🔚.
```

1. Expected on A: after each part, once 5 seconds pass without a new one, a `k` with the parts received so far, and
   after part 10 `HT1 k bob 4 1111011111`. The Messages page shows message 4 with the badge *receiving 9/10 parts*.
2. A: part 5. Expected at once: `HT1 k bob 4 1111111111`.
3. Expected on B: ten DMs `HT1 m alice 4 1/10 …` to `HT1 m alice 4 10/10 …`, at least 2 seconds apart, each carrying
   exactly the text of the line above after the part number. Put together, they give back the message:

   > Проверка HopTalk: десять частей, кириллица и эмодзи 👋🏽. Каждая часть не длиннее ста четырёх байт UTF-8 📡. Семью
   > (👨‍👩‍👧‍👦) и флаг (🇦🇺) нельзя резать посередине. Потерянную часть клиент отправит ещё раз 🔁. Получатель
   > подтверждает части набором единиц и нулей 🧩. Сервер пересылает части без изменений ✅. Ёжик, щука, жёлтый шарф,
   > чёрный кофе ☕. Съешь же ещё этих мягких французских булок, да выпей чаю 🍵. В сети MeshCore сообщения идут через
   > ретрансляторы (🛰). Сервер ждёт ответа от каждого устройства 📬. Конец проверки: сравните текст с исходным 🔚.

   (The quote wraps where this page wraps; the message itself has single spaces.)
4. B: `HT1 K alice 4 1111111111`. Expected: delivered, and `HT1 s bob 4 D` on A about 15 seconds later.
5. If the app refuses to send any of these lines, record the length it allows. The trackers are nRF52840 boards, so the
   truncation some ESP32 Bluetooth clients show above 153 bytes does not apply; if an ESP32-based companion is ever
   used, repeat this check with it.

## 7. A tracker switched off: retries, refresh and `F *`

The default retry strategy gives up on a silent device after about 25 minutes. Run this check with the defaults at
least once per release; for a quick repeat, set `RELAY_RETRY_INITIAL_PAUSE_SECONDS=5` and
`RELAY_RETRY_MAXIMUM_PAUSE_SECONDS=20` in `src/.env`, run `make --directory=src container-restart`, and put the
defaults back afterwards.

1. Switch tracker B off. A: `HT1 M bob 5 1/1 Пока тебя не было` → `HT1 k bob 5 1`.
2. Expected: the Traffic page shows the relay sending `m alice 5` to tracker B at about 0, 30, 90, 210, 450 and 930
   seconds, none with a firmware ACK; about 25 minutes after the first, tracker B's delivery of message 5 is *failed
   6/6* on the Messages page. A receives no `s`.
3. Switch tracker B on and connect phone B. B: `HT1 F alice`. Expected: `HT1 f alice 1`, then
   `HT1 m alice 5 1/1 Пока тебя не было`. B: `HT1 K alice 5 1`. Expected: delivered, and `HT1 s bob 5 D` on A.
4. Switch tracker B off again. A: `HT1 M bob 6 1/1 Первое` and then `HT1 M bob 7 1/1 Второе`. No need to wait for
   them to fail.
5. Switch tracker B on and type `HT1 F *` as soon as phone B is connected. Expected: `HT1 f * 2`, then
   `HT1 m alice 6 1/1 Первое`, and **not** `m alice 7` yet: a refresh sends one message at a time. (A retry round that
   was already due can still bring a copy of either message just before the `f`; after it, the order holds.)
6. B: `HT1 K alice 6 1`. Expected: now `HT1 m alice 7 1/1 Второе`. B: `HT1 K alice 7 1`. Messages → *Refresh
   sessions* shows the session completed.
7. Optional, stop on failure: switch B off, A sends two more messages, switch B on, B sends `HT1 F alice`, and switch B
   off right after the `f`. Expected: the first message is retried until it fails, the second is never sent, and the
   refresh session is *stopped*. A new `HT1 F alice` later starts over with fresh attempts.

## 8. A phone away from its tracker

1. Tracker B on; phone B disconnected from it (Bluetooth off, or the app closed).
2. A: `HT1 M bob 8 1/1 Телефон далеко` → `HT1 k bob 8 1`.
3. Expected: every round to tracker B gets a firmware ACK (*firmware ACK after … ms* on the Traffic page), because the
   tracker's radio received it, yet the delivery stays *pending* and the rounds continue 30, then 60, then 120 seconds
   apart: a firmware ACK is never taken as delivery.
4. After three rounds, reconnect phone B. Expected: the app shows the copies the tracker queued meanwhile. **Record how
   many copies arrive against how many rounds were sent.** Fewer copies than rounds means the tracker's offline queue
   dropped some.
5. B: `HT1 K alice 8 1`. Expected: delivered, the rounds stop, and `HT1 s bob 8 D` on A.

## 9. A tracker behind a repeater: stale routes in both directions

1. Tracker B in direct range of the relay. B: `HT1 Q alice` → `HT1 q alice 1`, and A: `HT1 M bob 9 1/1 Рядом` → B
   receives it and types `HT1 K alice 9 1`. Node → *Sync contacts now*, then note tracker B's route on the Contacts
   page (the route shown there is refreshed on every contact pass, at the latest every 10 minutes). Expected: a route
   with a hop count rather than *flood*; if it still says *flood*, exchange another message and sync again.
2. Move tracker B to where it hears only the repeater.
3. **Relay to tracker.** A: `HT1 M bob 10 1/1 Через ретранслятор`. Expected on the Traffic page: the first `m` to B goes
   *direct* over the old route and times out without a firmware ACK; its route reset is *performed*; the next round
   goes by *flood* and gets a firmware ACK; B receives the message and types `HT1 K alice 10 1`. Later sends to B go
   *direct* again, over a route through the repeater (after *Sync contacts now*, the Contacts page shows the new hop
   count).
4. **Tracker to relay.** B: `HT1 Q alice`. Tracker B's own stored route to the relay is stale too, so its DM is likely
   lost; the stock app may not fall back to flood by itself. If no answer comes, use the app's reset path action on the
   relay contact, and type the request again. Expected on the Traffic page: the DM arrives as *flood, 1 hop* (or more),
   with the badge *route reset before reply*, and B receives `HT1 q alice 1`. When a new route to B was learned in the
   30 seconds before the DM arrived, the relay skips that reset on purpose: record whether the badge appeared, and the
   times.
5. Move tracker B back into direct range and repeat steps 3 and 4. Record whether the route through the repeater keeps
   working, and any reset.
6. Record the *firmware ACK after … ms* values for direct sends with no hop, through the repeater, and by flood.

## 10. An asymmetric link

The problem HopTalk exists for: the relay's DMs reach the tracker, but the tracker's firmware ACKs do not come back
the same way, while its own DMs still reach the relay by another route.

1. Lower tracker B's transmit power to the lowest the app allows, and place it where it still hears the relay node
   directly while the relay hears it only through the repeater. Getting this right takes some trial; the Traffic page
   shows it when relay sends to B keep timing out while B's DMs arrive as *flood*.
2. A sends three messages a minute apart: `HT1 M bob 11 1/1 Один`, `HT1 M bob 12 1/1 Два`, `HT1 M bob 13 1/1 Три`.
   B answers each `m` with its `K` (after resetting its path to the relay, if its DMs stop arriving).
3. Expected: the relay's `m` to B times out without a firmware ACK, but B's `K` arrives, so the pending route reset is
   settled as *skipped: the device answered*, not *performed*; the following sends to B stay *direct*. Over the three
   messages, at most one reset is *performed* for B, and never one per packet. Each message is delivered even though no
   firmware ACK came back.
4. Put tracker B's transmit power back.

## 11. Node reboot, USB unplug and worker restart during traffic

Keep phone B away from tracker B during the first three cases (as in check 8), so that the relay keeps sending rounds
of whatever A sends. Before case 1, A sends `HT1 M bob 14 1/1 Перезагрузка`; before case 2,
`HT1 M bob 15 1/1 Кабель`; before case 3, the ten parts of check 6 under id 16 (in every line, replace ` 4 ` after
`bob` with ` 16 `). Afterwards, reconnect phone B and answer every message with its `K`.

1. **Node reboot.** Node → *Reboot node…*. Expected: the worker first takes every DM waiting on the node, waits until no
   packet awaits a firmware ACK, then reboots it; the node is back within about a minute and a half; the dashboard shows
   the clock set again (the XIAO lost it) and the relay log a clock correction; the rounds to B continue afterwards, and
   the message exists once on the Messages page.
2. **USB unplug.** Pull the node's cable for 30 seconds while a round is being sent, then plug it in again. Expected:
   the banner *The node is not connected*; after the replug the worker reconnects within 30 seconds, packets that were
   waiting for a firmware ACK show *acknowledgement timed out* with *route reset: dropped by a restart*, and the rounds
   continue. A DM that phone A sent while
   the node was unplugged was never received, because the node had no power: type it again.
3. **Worker restart.** `make --directory=src relay-restart` while the relay sends the parts of message 16. Expected:
   the relay log shows the worker stopping within 30 seconds and starting again; after the reconnect the delivery
   continues where it was, with no duplicate message and no attempt counted twice.
4. **DMs queued while the worker is away.** Stop the worker (`./scripts/docker-compose.sh stop relay` on the server, or
   Ctrl-C on the bridge on the Mac) while the node stays powered. A: `HT1 Q bob`. Wait a minute, then start it again
   (`make --directory=src container-up`, or the bridge). Expected: the worker takes the DM from the node's queue after
   the reconnect and answers `HT1 q bob 1`.
5. **Power loss right after a contact is added.** Add a contact (any third node, by card or pairing) and pull the cable
   within 5 seconds of it turning *on the node*. Plug it back in. The firmware writes new contacts to flash a few
   seconds late, so the node may have lost it. Expected: after the reconnect the contact is on the node again,
   re-added by the worker; the dashboard's counts of contacts on the node and in the database agree.

## 12. Load: the packet pool, pacing and radio timing

1. Switch tracker B off. A sends the ten parts of check 6 again under a new id: in every line, replace ` 4 ` after `bob`
   with ` 20 `. Then A: `HT1 M bob 21 1/1 Ещё одно`, `HT1 M bob 22 1/1 И ещё` and `HT1 M bob 23 1/1 Четвёртое`.
2. Expected while the relay retries to the silent tracker: on the Traffic page, sends at least 2 seconds apart; on the
   dashboard, *Awaiting a firmware ACK* never above 4; no more than three of the four messages in flight to tracker B
   at once, so message 23 is not sent until one of the others is confirmed or given up.
3. Switch tracker B on, and during the next round let both phones send requests at the same time (`HT1 Q alice`,
   `HT1 Q bob`, a few times each, 15 seconds apart). Expected: every request is answered, ahead of the waiting
   deliveries. If the node's packet pool fills up, some sends show *node error 3*; the relay backs off and sends them
   later without spending an attempt, and the relay log shows an error only if that happens ten times in a row.
4. B: `HT1 F alice`, and answer every message with its `K` until all four have arrived.
5. Record: the firmware ACK times seen during the burst compared with check 9, whether sends were held back by the
   region's duty cycle (the time between a send and its ACK growing during the burst), and any *acknowledgement timed
   out* followed by *route reset: skipped: a late acknowledgement*. Many of those mean the waits are too short for this
   mesh: the relay waits 1.2 times the node's suggested timeout, at least 3 and at most 60 seconds.

## 13. Reconfiguration with users present

**Keeping the relay's identity.** This is the check that the node's firmware exports and imports the private key.

1. On the dashboard, *Identity backup* shows *Stored*. **Write down the public key.**
2. Node → *Reconfigure node…*. Expected: the dialog says the relay's identity is backed up. *Start the setup wizard*,
   and go through check 2's steps 2 and 3. Expected: the wizard shows a new public key after the reset.
3. Configure as in check 2's step 4. Expected: *Keep the relay's identity* is shown and ticked, and the review says the
   relay's identity is kept, with the key written down. *Apply and reboot*.
4. Expected: *Restore the relay's identity* finishes with the key written down, before the settings are written;
   *Back up the new identity* is skipped (*The stored backup already holds the relay's identity.*); every other step
   as in check 2. The done page says the relay kept its identity and users need to do nothing.
   - *This node's firmware does not allow importing a private key* means the firmware was built without
     `ENABLE_PRIVATE_KEY_IMPORT`; *The node refused the relay's private key* means it refused the key (record the
     error number). Either way, record it, untick *Keep the relay's identity*, apply, and continue with the second part
     of this check: the relay cannot keep its identity with this firmware.
5. Expected on the dashboard: the public key written down in step 1, mode *Running*, *Identity backup* still *Stored*
   with its old date; on the Contacts page both trackers turn *being added*, then *on the node*.
6. Without touching either phone's contacts: phone B signs in again, `HT1 A bob hunter2222` → `HT1 a bob`. Phone A:
   `HT1 Q bob` → `HT1 q bob 1`, then `HT1 M bob 30 1/1 Тот же ключ` → `HT1 k bob 30 1`. Expected on B:
   `HT1 m alice 30 1/1 Тот же ключ`. B: `HT1 K alice 30 1`. Expected on A: `HT1 s bob 30 D`. The trackers never added
   a new card, so this shows the imported key signs, decrypts and acknowledges like the original one.

**A new identity:**

1. Node → *Reconfigure node…* → *Start the setup wizard*, and go through check 2's steps 2 to 5, this time with *Keep
   the relay's identity* unticked.
2. Expected: a new public key again, and *Back up the new identity* done; after the configuration, both trackers turn
   *being added* then *on the node* on the Contacts page, re-added to the new identity; `alice` and `bob` still exist
   with their devices.
3. Phone A: `HT1 Q bob`. Expected: no answer, because the phone still holds the relay's old identity.
4. On both phones delete the old relay contact and add the new card (check 3). Phone A: `HT1 Q bob`. Expected:
   `HT1 q bob 1`, with no new sign-in: the device's link to `alice` did not change.
5. Optional: start one more reconfiguration and press *Cancel setup* after the reset has completed. Expected within a
   few seconds: the banner *The attached node is not the configured one*, and nothing sent to anyone. The banner says
   the relay's identity is backed up, and its *Set up this node* leads to *Set up the attached node*, which starts the
   wizard over (another factory reset); finish it keeping the relay's identity, and the phones need nothing.

## Results

| Check | Passed | Notes and recorded numbers |
| --- | --- | --- |
| 1. USB connection and re-enumeration | | VID:PID, reconnect times, DTR |
| 2. Setup and the identity change | | Old and new key, clock offset |
| 3. Contact cards | | Card forms the app accepts |
| 4. Pairing | | |
| 5. Sign-in and the hand-testing session | | |
| 6. Cyrillic, emoji, one part and ten parts | | App length limit, if any |
| 7. A tracker switched off | | Time until failed |
| 8. A phone away from its tracker | | Copies received against rounds sent |
| 9. Stale routes in both directions | | ACK times: direct, through the repeater, flood |
| 10. An asymmetric link | | Resets performed for B |
| 11. Reboot, unplug, restart, power loss | | Reconnect times |
| 12. Load | | Node errors, ACK times under load |
| 13. Reconfiguration with users present | | Identity kept (yes, or the error), key before and after |
