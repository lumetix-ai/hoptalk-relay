# HopTalk Relay

The server half of HopTalk, a messenger built on [MeshCore](https://github.com/meshcore-dev/MeshCore) radio nodes.
A MeshCore companion node is plugged into the server over USB; the relay worker owns it, stores every message in
PostgreSQL and forwards it to every device of the recipient until each one confirms. An HTTPS admin panel on the local
network sets the node up, adds users' devices to it and shows every message and every direct message in both
directions. Everything runs in Docker, and nothing depends on the Internet once the images are built.

HopTalk exists to solve three problems:

1. **A messenger people can use without knowing MeshCore.** They sign in with a username and write to other
   usernames; the app and this server take care of the MeshCore side.
2. **Messages sent while the recipient's node was switched off.** The server keeps them and delivers them when the
   device comes back and asks for what it missed.
3. **Asymmetric links.** One repeater can be much stronger than its neighbour, so user A's direct message reaches
   user B while B's firmware acknowledgement never makes it back to A, and B's own messages may take another path and
   suffer the same fate. A MeshCore firmware ACK is therefore not a reliable delivery signal. HopTalk adds its own
   acknowledgements, carried by direct messages that may travel other routes, and never treats a firmware ACK as proof
   of delivery.

There is no HTTP API for the messenger. The HopTalk app talks to the server only through MeshCore direct messages (DMs),
using the plain-text protocol in [`docs/protocol.md`](docs/protocol.md). Until the app is published, the protocol can be
exercised by typing DMs in the stock MeshCore app
([Appendix A](docs/protocol.md#appendix-a-testing-by-hand-from-the-stock-meshcore-app)).

This document covers both machines you are likely to want: a Mac for development, and a Linux box on the local network
for actual use. They share the same repository and the same commands to build and start; the differences are a few
lines in `docker/.env` and how the USB node reaches the container.

## How it is put together

| Path | What lives there |
| --- | --- |
| `src/` | The application: Python 3.14, Django 6.1, HTMX, Tailwind CSS, the meshcore library, pytest |
| `src/Makefile` | Every command you run, as `make --directory=src <target>` |
| `docker/` | The image definition and the Compose files |
| `docker/.env` | How the stack is built and published: build target, ports, LAN address, database credentials, how the worker reaches the node |
| `src/.env` | How the application behaves: secret key, operator credentials, time zone, retry strategy, pacing, retention |
| `scripts/` | `docker-compose.sh`, `generate-tls-certificate.sh`, `serial-bridge.sh`, `install-agnix.sh` |
| `var/` | Runtime state, never committed: `database/` (production build), `tls/` and `authority/` |
| `docs/` | [`protocol.md`](docs/protocol.md), the DM protocol, and [`hardware-checks.md`](docs/hardware-checks.md), the release checklist on real radios |

Always drive Compose through `scripts/docker-compose.sh`. It pins the project name, picks the Compose files that match
`APP_BUILD_TARGET` and `MESHCORE_TRANSPORT` in `docker/.env`, detects the LAN address when `SERVER_ADDRESS` is empty,
and creates `var/database` and `var/tls` before Docker can create them as root.

### The three services

| Service | What it runs | Notes |
| --- | --- | --- |
| `app` | The admin panel: nginx and gunicorn (two processes, four threads each) under supervisord | The only service with published ports. Its entrypoint checks the TLS material, verifies `src/.env`, waits for the database and applies the migrations before it serves anything |
| `relay` | The relay worker, `python manage.py run_relay`: the only process that ever talks to the MeshCore node | The same image. It verifies `src/.env`, waits for the database and for the migrations the `app` container applies, then takes a PostgreSQL advisory lock, so a second worker never opens the node. It gets 30 seconds to stop, enough to record the messages it already took from the node |
| `database` | PostgreSQL 18 | Reachable only inside the Compose network |

The panel never talks to the node. It writes node commands, contacts and setup runs into the database and notifies the
worker in the same transaction; the worker listens for those notifications, also sweeps every 5 seconds, and writes
its own status back every 5 seconds. A status older than 20 seconds is what the panel reports as "the relay worker is
offline".

Every service is declared `restart: unless-stopped`.

### What the worker does in each mode

The dashboard shows the worker's relay mode. It is decided after every connection to the node, and it decides what
may touch the node:

| Mode | When | What the worker does |
| --- | --- | --- |
| *Running* | The attached node is the configured one | Everything: receives and answers DMs, delivers messages, keeps the node's contacts in step with the database, sends pairing adverts, and backs up the node's identity when no backup is stored (see *Keeping the relay's identity*) |
| *Not configured* | The node has never been set up, or a cancelled setup run left the relay's identity on a reset node it never configured | Only reads the node for the setup wizard |
| *Setup in progress* | The setup wizard is resetting or configuring the node | Only the wizard's own steps |
| *Identity mismatch* | The attached node has another public key than the configured one | Nothing that could harm either node: no sending, no receiving from its queue, no contact changes |
| *Disconnected* | The worker cannot reach the node | Reconnects with a back-off from 1 to 30 seconds |

DMs already recorded are processed in every mode, but replies go out only while *Running*.

### The two build targets

`APP_BUILD_TARGET` in `docker/.env` chooses which final image stage is built, and that choice also decides which
Compose files are merged.

| | `app-production` | `app-development` |
| --- | --- | --- |
| Application code | Baked into the image | Bind-mounted from `src/` into `app` and `relay` |
| Python dependencies | Installed during the build, without the development group | Installed during the build, with the development group: the checks and tests run in this image |
| Stylesheet | Built during the build, collected with hashed names | Built by you into `src/build/static` |
| Database | `var/database/` on the host | A named Docker volume |
| Django debug mode | Off | On: templates are read afresh on every request, and gunicorn restarts its workers when a Python file changes |
| Extra services | None | `node`, the Tailwind watcher, behind the `development` profile |

### The ports

The `app` container always listens on 8080 for plain HTTP, which only redirects, and on 8443 for HTTPS. `HTTP_PORT`
and `HTTPS_PORT` in `docker/.env` decide where those two are published on the host; without them Compose uses 80 and
443. `HTTPS_PORT` is also passed into the container, so the redirect and the cross-site request forgery check use the
port you actually published.

On the development Mac, the serial bridge listens on `127.0.0.1:5055` (`MESHCORE_TCP_PORT`), and the worker reaches it
through `host.docker.internal`. The database port is never published.

### Which file wins

Compose passes these to the containers as real environment variables: `POSTGRES_DB`, `POSTGRES_USER`,
`POSTGRES_PASSWORD`, `SERVER_ADDRESS` and `HTTPS_PORT` from `docker/.env`, `MESHCORE_TRANSPORT`, `MESHCORE_TCP_HOST`,
`MESHCORE_TCP_PORT` and, in serial mode, `MESHCORE_SERIAL_DEVICE` for the worker, plus `APP_ROLE`, `DJANGO_DEBUG`,
`POSTGRES_HOST` and `POSTGRES_PORT`, which the Compose files set themselves. The application parses `src/.env` on its
own, and a real environment variable always beats the same name in that file. Set the first group in `docker/.env`, and
everything else in `src/.env`. `DJANGO_DEBUG` in particular cannot be switched on from `src/.env`.

`src/.env` is mounted read-only and parsed without `$` expansion, which is why an Argon2 hash goes in as it is, without
quotes. Both the panel and the worker read their configuration once, when they start, and refuse to start while a
value is missing or out of range, naming the variable. A change to either file takes effect after
`make --directory=src container-restart`.

## Requirements

- Docker with Compose v2, verified with Docker 29.8.0 and Compose v5.5.1. Python, uv, Node and OpenSSL all run in
  containers, so the host needs none of them. Building the images needs the Internet; running them does not.
- GNU Make 3.82 or newer. Any current Linux has it (`sudo apt install make` on a minimal Debian or Ubuntu). macOS
  ships 3.81 and the Makefile refuses to run on it, so install a current one and let the shell find it first:

  ```bash
  brew install make
  echo 'export PATH="/opt/homebrew/opt/make/libexec/gnubin:$PATH"' >> ~/.zprofile
  ```

- On the development Mac, socat for the serial bridge: `brew install socat`. `make setup` also needs curl, which macOS
  has.
- The relay node: a Seeed XIAO nRF52840 with a Wio-SX1262, running the MeshCore USB serial companion firmware v1.17.1
  (companion protocol 13). Other companion nodes may work; the dashboard warns when the protocol version differs.

Every target lives in `src/Makefile` and is called from the repository root as `make --directory=src <target>`, with
arguments in `command="..."`. The targets are thin wrappers around `scripts/docker-compose.sh`; on a machine without
GNU Make, call that script directly instead. The fallback commands are in *Without Make* below.

## Deploying it

This is the path for the Linux machine on your network that the relay node is plugged into. Nothing is installed on
the host apart from Docker, and no dependency is resolved outside the image build.

### 1. Get the code

On a fresh Linux machine install Docker Engine first, following <https://docs.docker.com/engine/install/>, and add
your account to the `docker` group so none of the commands below need `sudo`. Clone over HTTPS
(`https://github.com/lumetix-ai/hoptalk-relay.git`) if that machine has no SSH key of its own.

```bash
git clone git@github.com:lumetix-ai/hoptalk-relay.git hoptalk-relay
cd hoptalk-relay
cp docker/.env.example docker/.env
cp src/.env.example src/.env
```

Copy both files before any other command: Compose mounts `src/.env` into the containers, and a missing file would be
created as an empty directory in its place.

### 2. Give the node a stable name

The node is a USB CDC-ACM device, `/dev/ttyACM0` or another number, and it re-enumerates, possibly under another
number, every time it reboots. A udev rule gives it a name that does not move and keeps ModemManager from probing it
as a modem.

Plug the node in and read its USB identifiers:

```bash
ls /dev/ttyACM*
udevadm info --query=property --name=/dev/ttyACM0 | grep --extended-regexp 'ID_VENDOR_ID|ID_MODEL_ID|ID_SERIAL'
```

For the XIAO nRF52840 they are expected to be `ID_VENDOR_ID=2886` and `ID_MODEL_ID=8044`. These come from MeshCore's
board definition and have not been confirmed on the real node yet, so use whatever `udevadm` printed if it differs.
Then create `/etc/udev/rules.d/99-meshcore-node.rules` with one line:

```
SUBSYSTEM=="tty", ATTRS{idVendor}=="2886", ATTRS{idProduct}=="8044", SYMLINK+="meshcore-node", ENV{ID_MM_DEVICE_IGNORE}="1"
```

Load it and check the result:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=tty
ls -l /dev/meshcore-node
stat --dereference --format %g /dev/meshcore-node
```

The symlink should point at the `ttyACM` device, and the last command prints the group that owns it, usually `20`
(`dialout`). Only one program can hold the node at a time, so stop anything else that opens it.

### 3. Say how it is published and how the node is reached

Edit `docker/.env`. For a machine reachable at `192.168.1.10` on the standard ports:

```dotenv
APP_BUILD_TARGET=app-production

PYTHON_VERSION=3.14.7
NODE_VERSION=24

HTTP_PORT=80
HTTPS_PORT=443

SERVER_ADDRESS=192.168.1.10

POSTGRES_DB=hoptalk_relay
POSTGRES_USER=hoptalk_relay
POSTGRES_PASSWORD=the-generated-password

MESHCORE_TRANSPORT=serial
MESHCORE_TCP_HOST=host.docker.internal
MESHCORE_TCP_PORT=5055

MESHCORE_SERIAL_DEVICE=/dev/meshcore-node
SERIAL_DEVICE_GID=20
```

- `SERVER_ADDRESS` is the address the certificate is issued for and the only address, besides `127.0.0.1` and
  `localhost`, the panel answers for. Use the address other devices will type. Give the machine a static lease on your
  router first: the certificate names the address, so a new address means a new certificate.
- `POSTGRES_PASSWORD` must be filled in before any Compose command works. Letters and digits only, because Compose
  expands `$` in this file:

  ```bash
  LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 32; echo
  ```

- `MESHCORE_TRANSPORT=serial` makes `scripts/docker-compose.sh` add `docker/docker-compose.serial-device.yml`. It gives
  the `relay` service the host's live `/dev` (so the node is found again after it re-enumerates), a device cgroup rule
  that adds USB serial devices to the few Docker allows by default, and `SERIAL_DEVICE_GID` as an extra group. The
  host's terminals (`/dev/pts`) and shared memory (`/dev/shm`) are hidden behind empty mounts, so the worker cannot
  reach your shell sessions, and it runs without any capability. The worker opens `MESHCORE_SERIAL_DEVICE` afresh on
  every reconnect. The two TCP values are not used in serial mode.
- `SERIAL_DEVICE_GID` is the number the `stat` command in step 2 printed.

### 4. Build the image

```bash
make --directory=src container-build
```

The build bakes your own account into the image (the Makefile passes your uid and gid as build arguments), and that
is what lets the containers write to the mounted directories and read the private key without any `chown`. Running it
later as a different account means rebuilding the image. The `app` and `relay` services share this one image.

### 5. Fill in the application secrets

Both processes refuse to start without a secret key and an operator, so both are produced before the first start.
Each of these runs in a throwaway container that skips the entrypoint, so neither needs a running application:

```bash
make --directory=src generate-secret-key
make --directory=src generate-admin-password
```

The first prints a `SECRET_KEY=` line. The second asks for the operator password twice, refuses anything shorter than
12 characters, and prints an `ADMIN_PASSWORD_HASH=argon2$argon2id$…` line. Put both into `src/.env` with the username
you want, as they are, without quotes:

```dotenv
SECRET_KEY=the-value-printed-above
ADMIN_USERNAME=ivan
ADMIN_PASSWORD_HASH=argon2$argon2id$v=19$m=102400,t=2,p=8$the-hash-printed-above
```

Keep the secret key. It signs the operator's session, so replacing it signs you out.

The other values in `src/.env` are already sensible, and the file explains every one of them. The ones worth knowing
about:

| Key | Default | Meaning |
| --- | --- | --- |
| `TIME_ZONE` | `UTC` | The zone the panel shows times in, such as `Australia/Melbourne`. The database stores UTC |
| `RELAY_RETRY_MAXIMUM_ATTEMPTS` | `6` | Delivery rounds per device, and attempts per receipt, before the server gives up |
| `RELAY_RETRY_INITIAL_PAUSE_SECONDS`, `RELAY_RETRY_BACKOFF_MULTIPLIER`, `RELAY_RETRY_MAXIMUM_PAUSE_SECONDS` | `30`, `2.0`, `600` | Rounds at 0, 30, 90, 210, 450 and 930 seconds, the pause doubling up to 10 minutes: a device that never answers is given up after about 25 minutes |
| `RELAY_DELIVERED_RECEIPT_DELAY_SECONDS` | `15` | How long a "delivered" receipt waits, so that a quick read sends only the "read" receipt |
| `RELAY_MAXIMUM_PACKETS_AWAITING_NODE_ACKNOWLEDGEMENT` | `4` | DMs that may wait for a firmware ACK at once; one place is kept for replies |
| `RELAY_MINIMUM_SECONDS_BETWEEN_SENDS` | `2.0` | The gap between two DMs the relay node sends |
| `RELAY_MAXIMUM_ACTIVE_DELIVERIES_PER_DEVICE` | `3` | Messages in flight to one device at a time |
| `RELAY_LOG_RETENTION_DAYS` | `30` | How long the traffic log (every DM in both directions) is kept. Messages themselves are kept |
| `RELAY_PAIRING_DEFAULT_DURATION_SECONDS`, `RELAY_PAIRING_DEFAULT_ADVERT_INTERVAL_SECONDS` | `120`, `30` | The defaults of the pairing form |

### 6. Issue the TLS certificate

```bash
make --directory=src generate-tls-certificate command="192.168.1.10"
```

Called without `command=` it reads `SERVER_ADDRESS` from `docker/.env`, and falls back to detecting the address of this
machine. It writes a local authority into `var/authority` and a server certificate into `var/tls`, for that address,
`127.0.0.1` and `localhost`. The authority is valid for ten years and the certificate for 398 days; reissuing the
certificate later does not disturb the clients that already trust the authority. Only `var/tls` is mounted into the
`app` container, so the authority's key never reaches the web server. OpenSSL runs in a container, so the host needs
none.

### 7. Start it

```bash
make --directory=src container-up
```

The first start creates the database and applies the migrations. `container-up` prints the state of the stack when it
is done: `app` reports `Up … (healthy)` once it serves the sign-in page, `database` reports `(healthy)`, and `relay`
reports `Up` (it has no health check of its own; the panel shows its state). If a service says `Restarting`, read its
log, because both entrypoints verify the configuration before anything else and say exactly what is missing:

```bash
make --directory=src container-logs
```

The worker's own log is easier to follow on its own:

```bash
make --directory=src relay-logs
```

#### Without Make

The targets above are these commands, and any machine without GNU Make can run them as they are:

```bash
export APP_UID=$(id -u) APP_GID=$(id -g)
./scripts/docker-compose.sh build --pull
./scripts/docker-compose.sh run --rm --no-deps --no-TTY --entrypoint python app \
    -c 'import secrets; print("SECRET_KEY=" + secrets.token_urlsafe(50))'
read -rsp 'Operator password: ' operatorPassword; echo
printf '%s' "${operatorPassword}" | ./scripts/docker-compose.sh run --rm --no-deps --no-TTY --entrypoint python app \
    -c 'import sys; from django.conf import settings; settings.configure(PASSWORD_HASHERS=["django.contrib.auth.hashers.Argon2PasswordHasher"]); from django.contrib.auth.hashers import make_password; print("ADMIN_PASSWORD_HASH=" + make_password(sys.stdin.read()))'
./scripts/generate-tls-certificate.sh 192.168.1.10
./scripts/docker-compose.sh up -d
./scripts/docker-compose.sh ps
```

### 8. Trust the certificate on every device

Copy `var/authority/authority.crt` to each device that will open the panel and install it as a trusted authority.
AirDrop and a USB stick work without the Internet.

- **macOS**: double-click the file, then in Keychain Access open it and set *Trust → Secure Sockets Layer* to
  *Always Trust*.
- **iPhone and iPad**: AirDrop or mail the file to the device, install the profile in *Settings → General → VPN &
  Device Management*, then turn on full trust for it in *Settings → General → About → Certificate Trust Settings*.
  The second step is easy to miss, and Safari keeps warning without it.
- **Android**: *Settings → Security → Encryption & credentials → Install a certificate → CA certificate*.
- **Firefox** keeps its own store: *Settings → Privacy & Security → Certificates → View Certificates → Authorities →
  Import*, and tick the website trust box.

Without this the browser warns on every visit. The certificate itself is fine; nothing signed by a local authority is
trusted until the authority is.

### 9. First sign-in and the setup wizard

Open `https://192.168.1.10/` and sign in with `ADMIN_USERNAME` and the password you hashed. As long as the node has not
been set up, the start page opens the setup wizard and every other page says that setup is required. The wizard needs
the worker to be connected to the node; until it is, it says what the worker is waiting for.

1. **Start setup.** The worker reads the node and the wizard shows everything it found: name, public key, firmware,
   model, radio, path hash size, multi-acks, automatic adding, contact count, clock and channel 0.
2. **Factory reset.** Setup always begins with a factory reset, and it is not reversible:
   - the node's identity is replaced by a new key pair, so its public key and its contact card change;
   - its name, radio settings, contacts, channels and every other setting are erased;
   - until setup configures it, it adds every node it hears as a contact;
   - **every user must add the server's new contact card** before they can use the relay again, unless the relay's
     identity is backed up and you keep it in the next step (see *Keeping the relay's identity*).

   To confirm you type the node's current name (the first 8 hexadecimal digits of its key when it has none), which
   also proves you are resetting the board you think you are. The node disconnects, reboots and comes back with its new
   identity within about a minute and a half. If it does not come back, unplug it, plug it in again and press
   *Retry*: a reset whose filesystem format failed leaves the node deaf until it is power-cycled.
3. **Configure.** Choose the node name (default `HopTalk Relay`, at most 31 bytes), a radio preset or *Manual entry*
   (frequency, bandwidth, spreading factor, coding rate), the path hash size and the transmit power. The presets are
   bundled with the application (see *Refreshing the radio presets*), nothing is fetched, and the one matching the
   node's current radio is marked. Use the
   same radio settings as your users' nodes. The relay always sets multi-acks to 2, manual adding of contacts, no
   automatic adding, no location in adverts and no telemetry. *Replace the Public channel with a private one* is off by
   default and best left off. On a reconfiguration with a readable backup of the relay's identity, *Keep the relay's
   identity* is ticked: the reset node gets the relay's key back and users need to do nothing. *Review*, then *Apply
   and reboot*.
4. **Configuring.** The worker gives the node the relay's identity back when you kept it, applies every value, reboots
   the node, reads everything back and compares it with what you asked for. Only when everything matches is the
   configuration saved. Otherwise it backs up the new identity's key with the configuration.
5. **Done.** The page shows the relay's contact card as a QR code and as a `meshcore://…` link. Users need it; see
   *Adding a user's device* below.

*Cancel setup* is offered until the configuration is applied. After a completed reset, cancelling leaves a node with a
new identity and no configuration, and the relay stays stopped until a setup run is completed. The same holds when an
attempt already gave the node the relay's identity back: the node then reports the relay's key with none of its
settings, the banner says it holds the relay's identity but was never configured, and it relays nothing until a setup
run is completed.

The dashboard's *Reconfigure node…* runs the same wizard later, with the same factory reset. With a stored backup of
the relay's identity the node gets its key back and users notice nothing; without one the relay gets a new identity,
and a new card every user has to add. The relay keeps delivering until you confirm the factory reset, and delivers
nothing from then until setup is completed.

### 10. Surviving a reboot

All three services are declared `restart: unless-stopped`, so they come back with Docker. Make sure Docker itself
starts at boot:

```bash
sudo systemctl enable --now docker
```

The node needs nothing: its settings live in its flash, the udev rule recreates `/dev/meshcore-node` whenever it
enumerates, and the worker keeps trying to connect until it appears. What the node keeps only in RAM is lost with its
power: its clock, and the DMs waiting for the worker. The worker sets the node's clock from the server's on every
connection, so the server needs a correct clock of its own; the users' apps resend anything that was not answered.

## Developing it

This is the Mac path when you intend to change the code. The application code is mounted from `src/`, and the
stylesheet has to exist on the host before the panel can render, which is what `setup` is for:

```bash
git clone git@github.com:lumetix-ai/hoptalk-relay.git hoptalk-relay
cd hoptalk-relay
make --directory=src setup
```

It downloads the `agnix` binary that checks the `.claude` configuration, copies both environment files from their
examples, writes a random `POSTGRES_PASSWORD` into `docker/.env`, issues a certificate, builds the development image,
installs the node packages, builds the stylesheet and writes a fresh `SECRET_KEY` into `src/.env`. Then it stops,
because one thing is left that it cannot invent:

```bash
make --directory=src generate-admin-password
```

Put the line it prints into `src/.env`, add an `ADMIN_USERNAME` of your own, and start the stack:

```bash
make --directory=src container-up
```

Then open `https://127.0.0.1:8444/`. The example configuration builds `app-development`, publishes ports 8081 and 8444
and detects the Mac's LAN address for the certificate, which also names `127.0.0.1` and `localhost`; change any of that
in `docker/.env`. Trust `var/authority/authority.crt` in Keychain Access as described in step 8 above.

`setup` is safe to run again, after a `git pull` for instance. It keeps the environment files, the database password,
the key and the authority that are already there, rebuilds the rest, and starts the stack when the operator is
configured.

### The serial bridge

Docker Desktop cannot pass a USB device into a container, so on the Mac the worker cannot open the node directly.
Instead it uses the meshcore library's TCP transport, whose frames are identical to the serial ones, and socat copies
the bytes between a local port and the USB device. Run the bridge in a terminal of its own and leave it running:

```bash
make --directory=src serial-bridge
```

It waits for a device matching `/dev/cu.usbmodem*`, prints `Serving /dev/cu.usbmodem… on 127.0.0.1:5055.` and accepts
one client, the worker, through `host.docker.internal`. Only the loopback address listens, so nothing else on the
network can talk to the node. When either side goes away socat exits and the bridge starts over, looking the device up
again, because the node may come back under a new name after a reboot or a factory reset. Ctrl-C stops it; the worker
then shows as disconnected and reconnects by itself once the bridge is back.

The port is 5055 rather than 5000 on purpose: macOS's AirPlay Receiver listens on port 5000, and a worker that reached
it instead of the bridge would wait for a node that never answers. The bridge refuses to start while anything else
listens on its port. To use another one, start it with `BRIDGE_PORT=5056 make --directory=src serial-bridge`, set
`MESHCORE_TCP_PORT=5056` in `docker/.env` and run `make --directory=src container-restart`.

The bridge reads these from the environment:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SERIAL_DEVICE_GLOB` | `/dev/cu.usbmodem*` | Which device to serve; it must match exactly one |
| `BRIDGE_PORT` | `5055` | The local port |
| `SERIAL_BAUD_RATE` | `115200` | Never 1200: the XIAO takes a 1200-baud connection as the signal to reboot into its bootloader |
| `SOCAT_BINARY` | `socat` | A socat that is not on the PATH |

Without the bridge running, the development worker keeps trying `host.docker.internal:5055` with a back-off, and the
panel shows that the node is not connected. That is expected, and everything except the node works.

### Changing things

- **Python code in the panel** is picked up by itself: gunicorn restarts its workers when a Python file changes.
- **Python code in the worker** needs `make --directory=src relay-restart`; the worker does not reload by itself.
- **Templates** are read afresh on every request in the development build; reload the page.
- **The stylesheet** is built by Tailwind from the classes the templates and scripts use. Rebuild it once with

  ```bash
  make --directory=src npm command="run build"
  ```

  or start the watcher, which sits behind the `development` profile and rebuilds `src/build/static/app.css` on every
  change:

  ```bash
  ./scripts/docker-compose.sh --profile development up -d node
  ./scripts/docker-compose.sh --profile development stop node
  ```

  Reload the page to see the result.
- **The panel's scripts** in `src/frontend/scripts/` are served as they are written; reload the page.
- **Migrations**: `make --directory=src manage command="makemigrations"`. The `app` container applies pending
  migrations on every start, and `make --directory=src manage command="migrate"` applies them at once.

Packages are managed with uv in a throwaway container, which only updates `pyproject.toml` and `uv.lock`. The image
owns the Python environment, so rebuild afterwards:

```bash
make --directory=src uv command="add segno"
make --directory=src container-build
make --directory=src container-up
```

Node packages go through the `node` service: `make --directory=src npm command="install --save-dev …"`.

### Checks and tests

Everything runs inside the development image, against PostgreSQL in the `database` service:

```bash
make --directory=src check-tests
make --directory=src check-style
make --directory=src check-types
make --directory=src check-templates
```

`make --directory=src check-tests command="tests/protocol -q"` runs part of the suite. `TEST_DATABASE_SUFFIX=alice`
before a test command gives that run its own test database (up to 30 letters, digits and underscores), so several runs
can share the database server at once.

No test needs a radio. The worker tests run the real meshcore library against a fake companion firmware and a
simulated mesh of devices (`src/tests/worker/fake_node`), and the end-to-end scenarios in `src/tests/scenarios` put
reference HopTalk clients (`src/tests/worker/simulated_hoptalk_client.py`) on those devices, in front of the real
worker. What only real radios can show is in [`docs/hardware-checks.md`](docs/hardware-checks.md), the checklist to go
through before a release.

`make --directory=src check-all` runs all of these, plus the lock file check, the pip-audit vulnerability check, the
Dockerfile lint and the `.claude` configuration check, in parallel. The same set runs in continuous integration on every
push, together with a job that builds the production image and checks that the sign-in page and everything it
references are served from the image itself. `make --directory=src fix-all` applies what ruff and djlint can fix by
themselves.

A second, isolated development stack (its own containers, image and database volume) can run beside the first. In a
terminal of its own, give it a project name and free ports; only the development target allows it, and only one stack
should use the serial bridge:

```bash
export HOPTALK_COMPOSE_PROJECT_NAME=hoptalk-relay-second HTTP_PORT=8091 HTTPS_PORT=8454
make --directory=src container-build
make --directory=src container-up
```

### The Make targets

`make --directory=src` on its own prints the whole list, and abbreviations resolve as long as they are unambiguous, so
`make --directory=src c-u` runs `container-up`. A target that takes a command also accepts it as the next word:
`make --directory=src manage "migrate --plan"`.

| Target | What it does |
| --- | --- |
| `container-up`, `container-down`, `container-restart`, `container-ps`, `container-logs` | Lifecycle of `app`, `relay` and `database` |
| `container-build` | Build the image, exporting your uid and gid |
| `container-execute command="..."` | Run something in the `app` container, `bash` by default |
| `relay-logs`, `relay-restart` | Follow the worker's log; restart only the worker |
| `serial-bridge` | Expose the USB node on `127.0.0.1:5055` for the worker (macOS development) |
| `manage command="verify_configuration"` | Run a Django management command in the `app` container |
| `uv command="add segno"` | Run uv in a throwaway container; rebuild the image afterwards |
| `npm command="run build"` | Run npm in the `node` service (development only) |
| `check-all`, `check-tests`, `check-style`, `check-types`, `check-templates`, `check-lock`, `check-security`, `check-dockerfile`, `check-claude` | Checks |
| `fix-all`, `fix-style`, `fix-templates` | Apply what ruff and djlint can fix by themselves |
| `generate-secret-key` | Print a new `SECRET_KEY` line |
| `generate-admin-password` | Ask for the operator password twice and print its Argon2 hash line |
| `generate-tls-certificate command="192.168.1.10"` | Reissue the certificate |
| `update-radio-presets` | Download the current MeshCore radio presets into `src/node/radio_presets.json` (needs the Internet) |
| `setup` | Prepare a development checkout |

`manage`, `container-execute` and the Python checks run inside the `app` container and start the stack first if it is
not running. `uv`, `update-radio-presets`, `generate-secret-key` and `generate-admin-password` use a throwaway
container that skips the entrypoint, so they also work while the application cannot start yet.

### Refreshing the radio presets

The setup wizard offers the radio presets the MeshCore clients show, from a snapshot bundled in
`src/node/radio_presets.json`; the relay never fetches them. When the community publishes new ones, refresh the
snapshot on a machine with Internet access:

```bash
make --directory=src update-radio-presets
```

It downloads `https://api.meshcore.nz/api/v1/config`, refuses a response with a preset the firmware or the wizard
cannot use (a frequency outside 150–2500 MHz or with more than 3 decimals, an unknown bandwidth, a spreading factor
outside 5–12, a coding rate outside 5–8, a path hash size other than 1–3, a repeated title), and rewrites the snapshot
only when the presets changed, printing the added, removed and changed titles. Review the change with `git diff`,
commit it, and rebuild the image for production.

## Everyday operations

All of these are run from the repository root, or done in the panel.

| Task | How |
| --- | --- |
| State of the stack | `make --directory=src container-ps` |
| Follow every log | `make --directory=src container-logs` |
| Follow the worker's log | `make --directory=src relay-logs` |
| Restart everything | `make --directory=src container-restart` |
| Restart only the worker | `make --directory=src relay-restart` |
| Stop | `make --directory=src container-down` |
| Shell in the `app` container | `make --directory=src container-execute` |
| Check the configuration and print the values in effect | `make --directory=src manage command="verify_configuration"` |
| Put the configured settings back on the node | Node → *Re-apply configured settings* |
| Push the contacts to the node now | Node → *Sync contacts now* |
| Reboot the node | Node → *Reboot node…* |
| Make the relay known to nodes nearby | Node → *Send advert (zero-hop)*, or *Send advert (flood)* to reach through repeaters |
| Freshly signed contact card | Node → *Regenerate contact card*, or Contacts → *Regenerate card* |
| Remove a device, or a user with all their devices | Contacts or Users → the delete button in the row |
| See every DM in both directions, decoded | Messages → *Traffic* |
| Reset and reconfigure the node | Node → *Reconfigure node…* (with *Keep the relay's identity* users do nothing; otherwise every user adds the new card) |
| Put the relay onto a replacement board | Attach the board, follow the banner's *Set up this node*, and keep the relay's identity |

Anything with awkward quoting is easier through the script the targets wrap, `./scripts/docker-compose.sh exec app
<command>`, with `APP_UID` and `APP_GID` exported.

### Reading the log

The worker logs to standard output, which `make --directory=src relay-logs` follows. Errors are failed handshakes,
failed contact additions, a full contact table and internal errors; warnings are connection attempts that failed (the
node unplugged or the bridge not running), an identity mismatch, settings that drifted, a node clock ahead of the
server, an unexpected firmware protocol and disconnects; the rest is connections, clock corrections, contact
reconciliations, account switches and finished node commands. The panel shows the current
state and the last error, not the history: the history is only in this log. Sign-in passwords never reach a log line;
they are replaced by `********`.

Docker keeps a container's log until the container is replaced, which `container-up` does after every build. By
default it never rotates it, and an open panel tab polls every few seconds, each poll one line in the web log. To cap
the logs of every container, put these keys into `/etc/docker/daemon.json` (create the file if it does not exist) and
restart Docker; they apply to containers created afterwards:

```json
{
    "log-driver": "json-file",
    "log-opts": { "max-size": "20m", "max-file": "5" }
}
```

### Adding a user's device

Two nodes can exchange DMs only after each has added the other as a contact, and the relay node adds nothing by
itself: its automatic adding is off. So every device takes two steps.

**The user adds the relay.** They scan the QR code or paste the `meshcore://…` link from the Contacts page (or the last
page of the setup wizard) in their MeshCore app. Send it to them any way you like; the card is signed by the node and is
not secret.

**You add the user's device**, in one of two ways:

- **By card.** The user copies their node's contact card from their MeshCore app and sends it to you. On the Contacts
  page, paste it into *Add by contact card* and press *Check card*. The preview shows the name, the full public key,
  the node type, the advert time and whether the signature is valid, and it refuses a node that is not a chat node, one
  that is already a contact, one whose first 6 bytes of key collide with another contact, the relay's own card, and any
  card once the 350 places are taken. *Add contact* saves it; the row shows *being added* until the worker has put it
  on the node, then *on the node*, or *not added* with the reason.
- **By pairing**, when the user's node is within radio range. On the Contacts page, start pairing: the relay sends an
  advert every 30 seconds for 2 minutes by default (up to 10 minutes), zero-hop unless you tick *Flood adverts*.
  Ask the user to send an advert from their app. Every node the relay hears appears under *Heard nodes* with its
  name and full public key; compare the key with the one the user reads in their app, because a name is whatever the
  node claims. The *+* button asks *Do you really want to add this contact?*, and *Yes, add* adds it exactly as a card
  would. Adding stays possible for 15 minutes after the session ends.

A device that is on the node but has not signed in yet is listed as *not registered*. It can send only a sign-in, and
it takes one of the node's 350 places until you delete it.

### What a user needs

- A MeshCore companion node of their own, and the HopTalk app. Until the app is available, the stock MeshCore app and
  [Appendix A](docs/protocol.md#appendix-a-testing-by-hand-from-the-stock-meshcore-app) of the protocol will do for
  testing.
- The relay's contact card, added to their node.
- Their own node added to the relay by you, by card or by pairing. Until then the relay cannot even decrypt their DMs,
  and nothing comes back.
- A username of 3 to 16 Latin letters and digits, and a password of 8 to 64 characters that fits in 64 bytes. There
  is no registration step: the first sign-in with a free username creates the account, and signing in with the same
  username and password on another device links that device too. Usernames are case-insensitive.

After a reconfiguration that did not keep the relay's identity, the relay has a new identity: every user adds the new
card, and the old contact stops answering.

### Keeping the relay's identity

A factory reset, the first step of every setup run, gives the node a new key pair. So that users do not have to add a
new card after every reconfiguration, the relay keeps an encrypted backup of its node's private key in the database
(`node_setting`, key `node.identity_backup`):

- **When it is taken.** The worker takes it right after it connects to the configured node, whenever there is none, it
  no longer decrypts, or it belongs to another key; a setup run that gives the node a new identity takes the backup of
  that identity and saves it with the configuration. The Node page shows *Identity backup*: stored with its date, not
  stored, not allowed by the firmware, or unreadable with the reason.
- **How it is protected.** The key is encrypted with AES-256-GCM under a key derived from `SECRET_KEY` in `src/.env`,
  bound to its public key. It is never logged, shown in the panel or offered for download. A database dump therefore
  holds the relay's identity in a form only `src/.env` unlocks: keep `src/.env` with the dumps, and both where only
  you can read them. The node's link is not protected at all, though: a firmware with the export enabled hands its key
  to any program that can talk to its serial port (on the Mac, to anything that connects to the bridge's port before
  the worker does), and a reconfiguration that keeps the identity sends the key, unencrypted, to whatever node the
  worker is connected to. The key is therefore only as safe as `src/.env`, the database, and the access to the node's
  USB link or the bridge's port. Before a reconfiguration, check that the bridge's terminal shows no client you do not
  recognise.
- **Only a matching key counts.** A private key is backed up only when it derives the public key the node reports right
  after the export, and imported only when it derives the relay's configured key; a backup whose key does not is shown
  as damaged and never offered.
- **When `SECRET_KEY` changes**, the stored backup no longer decrypts. The Node page says so, and a reconfiguration can
  no longer keep the identity, until the worker takes a new backup from the attached node, which it does at its next
  connection to the configured node.
- **A reconfiguration** offers *Keep the relay's identity* in the configuration step, ticked, whenever the backup is
  readable. The configuration first imports the key into the reset node, and every later check expects the relay's own
  key. The reconciliation adds every contact to the node again, and users' nodes keep working with the same card.
  Unticked, the node keeps the new key and every user adds the new card.
- **A replacement board.** Attach it: the banner says the attached node is not the configured one. *Set up this node*
  runs the wizard on it, and keeping the relay's identity puts the relay's key onto the new board. Do not power the old
  board up again unless it has been reset: two nodes with one identity confuse the mesh.

Both need a firmware that allows exporting and importing the private key (`ENABLE_PRIVATE_KEY_EXPORT` and
`ENABLE_PRIVATE_KEY_IMPORT`, on in the stock companion builds). A node that refuses the export is reported on the Node
page; one that refuses the import fails the configuration step with the reason, and you can run it again without
keeping the identity.

### Deploying a new version

```bash
git pull
make --directory=src container-build
make --directory=src container-up
```

`container-up` replaces the `app` and `relay` containers. The `app` entrypoint applies the migrations on every start,
and the worker waits for them before it opens the node. The old worker gets 30 seconds to record the DMs it already took
from the node and to close the link; anything else in flight is picked up again after the restart, because every
delivery and every schedule lives in the database.

### Changing the operator password

```bash
make --directory=src generate-admin-password
```

It asks twice, refuses anything shorter than 12 characters, and prints the line to paste into `src/.env`. Restart with
`make --directory=src container-restart`. Changing the password or the username signs every browser out.

### Changing the retry strategy and other settings

Edit `src/.env` and run `make --directory=src container-restart`: the panel and the worker read the file only when
they start. The dashboard's *Configuration* card shows the values the worker is running with, and
`make --directory=src manage command="verify_configuration"` prints them. New pauses apply at once; a delivery keeps the
number of attempts it started with until it is restarted.

### Reissuing the certificate

Needed when the machine's address changes, or once the 398 days are up:

```bash
make --directory=src generate-tls-certificate command="192.168.1.20"
make --directory=src container-restart
```

If the address changed, change `SERVER_ADDRESS` in `docker/.env` before the restart as well. The authority is reused,
so the clients that already trust it need no attention. An authority still kept in `var/tls` is moved to
`var/authority` the first time the script runs, and the clients keep trusting it.

### Backups

Everything the relay knows is in PostgreSQL: users, devices, messages, the node's configuration and the encrypted
backup of its identity. Take a dump with
`pg_dump` in the `database` container. `--no-TTY` keeps the binary dump intact on its way to the host, and the single
quotes let the container's own shell fill in its user and database names:

```bash
./scripts/docker-compose.sh exec --no-TTY database \
    sh -c 'pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom' \
    > "hoptalk-relay-$(date +%Y-%m-%d-%H%M%S).dump"
```

It is consistent while the relay runs. To see what a dump holds:

```bash
./scripts/docker-compose.sh exec --no-TTY database pg_restore --list < hoptalk-relay-2026-09-26-031700.dump
```

To restore, stop the panel and the worker, replace the database's content in one transaction, and start them again.
`--clean --if-exists` drops what the dump contains before recreating it, and `--single-transaction` rolls everything
back if any statement fails:

```bash
./scripts/docker-compose.sh stop app relay
./scripts/docker-compose.sh exec --no-TTY database \
    sh -c 'pg_restore --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --clean --if-exists --no-owner --single-transaction --exit-on-error' \
    < hoptalk-relay-2026-09-26-031700.dump
make --directory=src container-up
```

On a new machine, deploy as above up to step 7 first, so an empty database exists, then restore into it the same way.

A dump holds every message in clear text and every user's password hash; keep it where only you can read it. Keep a
copy of `src/.env`, `docker/.env` and `var/authority` as well: without `var/authority/authority.key` a new authority
has to be trusted on every device again, while `var/tls` can always be reissued from it. The node's private key is in
the dump, encrypted with a key derived from `SECRET_KEY`: restored next to another `src/.env`, it cannot be decrypted,
and a replaced or reset node then means a new card for every user (see *Keeping the relay's identity*).

To take a dump every night at three, add a line like this with `crontab -e` (cron needs `%` escaped):

```
0 3 * * * cd /home/ivan/hoptalk-relay && ./scripts/docker-compose.sh exec --no-TTY database sh -c 'pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom' > /home/ivan/backups/hoptalk-relay-$(date +\%A).dump
```

The day name in the file name keeps one dump per weekday, overwritten a week later.

## The DM protocol in brief

The full specification, with the grammar, every error and sequence charts, is [`docs/protocol.md`](docs/protocol.md).
Its test vectors, which the HopTalk app runs too, are in `src/tests/protocol/vectors/`.

Every DM is `HT1 <letter> <fields>`: upper-case letters go from a client to the server, lower-case letters from the
server to a client.

| Client sends | Meaning | Server answers |
| --- | --- | --- |
| `HT1 A <username> <password>` | Register or sign in; a free username creates the account | `a <username>` or `e <code> …` |
| `HT1 Q <username>` | Does this user exist? | `q <username> 1` or `0` |
| `HT1 M <recipient> <id> <n>/<count> <text>` | One part of a message, up to 10 parts of 104 bytes | `k <recipient> <id> <received parts>`, such as `k bob 42 101` |
| `HT1 K <sender> <id> <received parts>` | The device has these parts of an `m` the server sent | Nothing |
| `HT1 R <sender> <id>` | The user read this message | `r <sender> <id>` |
| `HT1 C <recipient> <id> <D or R>` | The device got this receipt | Nothing |
| `HT1 F <peer>` or `HT1 F *` | Send me what I missed from this peer, or from everyone | `f <peer> <count>`, then the messages as `m` parts |

The server pushes `m <sender> <id> <n>/<count> <text>` to every device of the recipient, and `s <recipient> <id> D` or
`R` ("delivered", "read") to every device of the sender, each retried until that device confirms or the attempts run
out. A DM carries at most 150 bytes, so any message fits the relay's forwarding byte for byte. The server never answers
acknowledgements, lower-case letters, errors or any other text, so two servers or a chat bot cannot keep each other
busy; other text is only logged. A firmware ACK is never taken as delivery: only `K`, `R` and `C` count.

## Where the data ends up

```
var/
├── database/                  PostgreSQL's data directory (production build), under 18/docker
├── authority/                 no container mounts it
│   ├── authority.crt          install this on the clients
│   ├── authority.key          the private key of the local authority
│   └── authority.srl          the authority's serial number counter
└── tls/                       mounted read-only into the app container
    ├── server.crt, server.key what nginx serves
    └── fullchain.crt
```

| Tables | What they hold |
| --- | --- |
| `users`, `contacts` | Accounts with their Argon2 password hashes, and every contact of the relay node: users' devices and devices that have not signed in yet |
| `messages`, `message_deliveries`, `receipt_notifications`, `refresh_sessions` | Every message with its text and parts, and its delivery and receipt state per device. Kept until the operator deletes the sender or the recipient; an incomplete upload is dropped 24 hours after its last part |
| `inbound_direct_messages`, `outbound_packets` | The traffic log, every DM in both directions. Kept for `RELAY_LOG_RETENTION_DAYS`; sign-in passwords are stored redacted |
| `node_setting`, `node_setup_runs`, `node_commands`, `worker_status` | The node's configuration with the encrypted backup of its private key, the setup wizard's runs, the panel's commands to the worker (kept 90 days) and the worker's live status |
| `pairing_sessions`, `heard_adverts` | Pairing sessions and the adverts heard during them (kept 7 days after the session) |
| `operator_login_attempts`, `django_session` | The sign-in throttle (kept one day) and the operator's sessions (deleted once expired) |

The worker's maintenance task does the pruning every 10 minutes.

In the development build the database is a named Docker volume instead, because the macOS file sharing layer does not
carry PostgreSQL's ownership changes over faithfully. `docker volume rm hoptalk-relay_database`, after
`make --directory=src container-down`, throws that database away; the production bind mount is untouched by it.

## Security notes

- Keep it on the local network. There is no HSTS (browsers ignore it for an address), the certificate comes from a
  local authority, and there is exactly one operator; nothing here is meant to face the Internet. The database is not
  published, and the development bridge listens on the loopback address only.
- `var/authority/authority.key` can sign a certificate for any name, and every device you set up would trust it. It
  is readable by your account only, and no container mounts `var/authority`: nginx and gunicorn run as your account,
  so a key in the mounted `var/tls` would be readable from the network-facing container. Keep it where it is.
- The operator's password is an Argon2 hash in `src/.env`. Sessions end after 2 hours without activity and after 12
  hours in any case, and the cookies are secure, HTTP-only and same-site strict. More than 5 failed sign-ins from one
  address within 15 minutes lock that address out for the rest of the window. Every destructive action sits behind a
  confirmation, and the ones that cannot be undone (a factory reset, deleting a user) ask you to type a name.
- The sign-in throttle tells clients apart only by the address the container sees. Docker Desktop relays every
  connection through a proxy, and so does Docker on Linux for a connection over the loopback address or IPv6, so all
  of those clients arrive from one address: there, any host on the LAN can hold the sign-in page at *Too many
  attempts* for everyone. The ports are published on IPv4 only for this reason. A Linux server reached at
  `SERVER_ADDRESS` over IPv4 sees each client's own address. To lift a lockout before its 15 minutes are up, empty the
  table of attempts:

  ```bash
  ./scripts/docker-compose.sh exec database \
      sh -c 'psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --command="DELETE FROM operator_login_attempts"'
  ```
- `DJANGO_DEBUG` is set by the Compose files: off for the production build, on only for the development one.
- Users' passwords travel inside MeshCore DMs, protected by the encryption MeshCore shares between two contacts (AES-128
  with a 2-byte MAC) and nothing more. The server stores them as Argon2 hashes and redacts them from the traffic log,
  the panel and every log line. Five wrong passwords from one device lock that device's sign-ins for up to 15 minutes.
- Message text is stored as it arrives, unencrypted; there is no end-to-end encryption yet. Anyone with the database or
  a dump reads every message.
- Nobody can talk to the relay without the operator: its node adds no contact by itself, and a device the operator did
  not add cannot even be decrypted.
- The node's private key is stored in `node_setting`, encrypted with AES-256-GCM under a key derived from `SECRET_KEY`.
  Whoever has both the database (or a dump) and `src/.env` can act as the relay on the mesh. The key never appears in
  a log, the panel, a node command's progress or the worker's status. It does cross the node's USB link in the clear:
  a firmware with the export enabled gives it to any program that can talk to the node's serial port (on the Mac, the
  bridge's port while the worker is not connected), and keeping the identity in a reconfiguration sends it to whatever
  node the worker is connected to. So the key is only as safe as `src/.env`, the database, and access to that link.
- The panel serves every script, stylesheet and icon itself, under a strict content security policy, and the relay never
  fetches anything from the Internet; the radio presets are bundled, and only `make update-radio-presets`, run by
  hand, downloads new ones.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `GNU Make 3.82 or newer is required, and this is 3.81` | The shell found the macOS Make. Put `/opt/homebrew/opt/make/libexec/gnubin` first on the PATH |
| `Set POSTGRES_PASSWORD in docker/.env` | Compose refuses to read its files without it. `make setup` fills it in on a development machine; on a server, generate one as in step 3 |
| A container sits in `Restarting` | Read `make --directory=src container-logs`. The entrypoint names what is missing |
| `The configuration in src/.env cannot be used:` followed by a list | Each line names a variable and what is wrong with it, such as `SECRET_KEY is empty` or `ADMIN_PASSWORD_HASH must be an Argon2 hash`. Fix it in `src/.env` and restart |
| `/etc/nginx/tls/fullchain.crt is missing` | Step 6 was skipped. `make --directory=src generate-tls-certificate` |
| `/etc/nginx/tls/server.key cannot be read as app` | The image was built by another account than the one that issued the certificate. Rebuild with `make --directory=src container-build` |
| `.env` appears as a directory inside the containers | `src/.env` did not exist when Compose first started. Remove the directory, create the file, and run `make --directory=src container-restart` |
| `Bind for 0.0.0.0:443 failed: port is already allocated` | Something else holds the port. Change `HTTPS_PORT` (or `HTTP_PORT`), or stop the other service |
| The browser warns about the certificate | The authority is not trusted on that device yet: step 8. On an iPhone, check the second switch under *Certificate Trust Settings* |
| `Bad Request (400)` | The panel answers only for `SERVER_ADDRESS`, `127.0.0.1` and `localhost`. Open it by the address in `docker/.env`, or change that address and reissue the certificate |
| `Forbidden (403)` with a CSRF failure when signing in | The browser reached the panel on another address or port than `SERVER_ADDRESS` and `HTTPS_PORT`, for instance through a port forward |
| *Too many attempts, try again in N minutes* on the sign-in page | More than 5 failed sign-ins from this address in 15 minutes. Wait, or sign in from another machine. Behind Docker Desktop every client shares one address; *Security notes* shows how to lift the lockout |
| Banner *The relay worker is offline* | The worker has not reported for 20 seconds. `make --directory=src container-ps` shows whether `relay` runs, and `make --directory=src relay-logs` why it stopped |
| Banner *The node is not connected* | The worker runs but cannot reach the node; the banner shows the last error. On the Mac, `Connecting to the node over tcp host.docker.internal:5055 failed: … Connect call failed …` means nothing listens on the port: is `make --directory=src serial-bridge` running in its own terminal? On the server, does `ls -l /dev/meshcore-node` show the node, and is `SERIAL_DEVICE_GID` its group? Is another program holding the port? |
| The bridge says `Port 5055 is already in use by: …` | Stop that process, or pick another port as described under *The serial bridge* |
| The bridge says `socat is not installed` | `brew install socat` |
| The bridge says `Several devices match /dev/cu.usbmodem*` | More than one USB serial device is plugged in. Set `SERIAL_DEVICE_GLOB` to the node's device |
| The bridge keeps saying `Waiting for a device matching /dev/cu.usbmodem*` | The node is not plugged in, or not powered. A node that stays missing after a reset or a reboot needs to be unplugged and plugged in again |
| Banner *The attached node is not the configured one* | Another board is attached, or the node was reset outside the panel. The relay stops touching it. Attach the configured node again, or follow the banner's *Set up this node* to run setup on the attached one: with a stored identity backup, keep the relay's identity and users need to do nothing; otherwise every user adds the new card |
| Node page: *Identity backup* not stored, *This node's firmware does not allow exporting its private key* | The firmware was built without `ENABLE_PRIVATE_KEY_EXPORT`. Relaying is unaffected, but a reconfiguration gives the relay a new identity. Flash a companion build with the export enabled, and the worker takes the backup at its next connection |
| Node page: *Identity backup* unreadable | `SECRET_KEY` in `src/.env` changed since the backup was taken (a dump restored next to another `src/.env`, or a new key), or the stored value was altered or holds a key that does not belong to the relay's public key. Put the old `SECRET_KEY` back, or let the worker take a new backup: it does so at its next connection to the configured node (`make --directory=src relay-restart`) |
| The configuration step fails with *This node's firmware does not allow importing a private key* | The firmware was built without `ENABLE_PRIVATE_KEY_IMPORT`. Flash one with the import enabled and apply again, or untick *Keep the relay's identity* (every user then adds the new card) |
| The configuration step fails with *The node refused the relay's private key* | Error 6: the firmware considers the key invalid; error 5: it could not save it to its flash. Apply again; if it keeps failing, untick *Keep the relay's identity*. A node that already holds the relay's key after a failed attempt still counts as the reset node, and applying again goes on with that key whether the box is ticked or not; only a new factory reset (cancel and start again) gives it a new identity |
| Banner *The node holds the relay's identity but was never configured* | A setup run was cancelled after it had given the reset node the relay's identity back, so the node has its factory settings and no contacts. Follow the banner's *Start setup* and keep the relay's identity: after the reset the backup gives the node its key back, and users need to do nothing. The relay cannot tell that node from the original board, so the original shows the same banner if it is attached again after such a cancelled setup on a replacement board; set it up the same way |
| The wizard says *The node did not come back after the reset* | Unplug the node, plug it in again, and press *Retry*. A failed filesystem format leaves the node deaf until it is power-cycled |
| The wizard says the node ignored the reset | The firmware did not accept the reset command, which happens when a firmware version expects another payload. Check the version against *Requirements* and [`docs/hardware-checks.md`](docs/hardware-checks.md) |
| Banner *The node's settings differ from the configured ones* | Something changed a setting on the node. Node → *Re-apply configured settings*. Manual adding and automatic adding are corrected by the worker every time it connects, because they protect the contact table |
| Banner *Unexpected firmware protocol version* | The node runs a firmware other than the tested one. Relaying continues; go through [`docs/hardware-checks.md`](docs/hardware-checks.md) before relying on it |
| A contact stays *being added* | The worker adds contacts only while the mode is *Running*. Look at the banners; once the node is connected and configured, Node → *Sync contacts now* runs a pass at once |
| A contact shows *not added* | Its row says why. *The node's contact table is full.* means all 350 places are taken: delete devices that never registered or are no longer used. The worker retries failed contacts on every pass |
| A user's DMs get no answer at all | Their node is not a contact of the relay yet, or they still have the card from before a reconfiguration. Messages → *Traffic* shows every DM the relay received; if theirs is not there, the relay never decrypted it |
| A sign-in gets `e RATE_LIMITED` | Five wrong passwords from that device within 15 minutes. It clears by itself when the window ends |

## Known limitations

These are deliberate trade-offs, chosen to keep the server simple.

- **Keeping the relay's identity needs the firmware's key export and import.** Setup always starts with a factory reset,
  which gives the node a new key pair. Only a node whose firmware lets the relay back up its private key, and a reset
  node that accepts it back, keep the relay's identity; otherwise every user has to add the new card, and a replaced
  board means the same. The backup is only as safe as `src/.env`.
- **One node, one worker, 350 contacts.** There is no second relay to fail over to, and a device that never signed in
  still takes one of the 350 places until you delete it.
- **Deleting deletes.** Deleting a user deletes every message they sent or received, including the other users'
  conversations with them on the Messages page, and anything still undelivered to or from them is never delivered.
- **Sign-in is simple.** A retried wrong password counts again, so a typo on a lossy link reaches the rate limit sooner,
  and while a device is locked even a retry of a successful sign-in is refused until the window ends. The lock is per
  device, not across devices. A mistyped username silently creates a new account.
- **A device can switch accounts without the operator.** Signing in with another user's correct password moves the
  device to that user. A message already on its way when that happens can land in the new account.
- **A device that is off still costs airtime.** Every round resends every part it has not confirmed, at the normal
  pace, for about 25 minutes. When a user's phone is away from their node, the node keeps acknowledging and queueing
  those copies, and once its queue is full it drops newer messages.
- **A refresh can wait.** The first message of a refresh waits for a free place while three other deliveries to that
  device wait between rounds, which can take up to the longest pause (10 minutes) although the device has just shown it
  is reachable.
- **Replies always go first.** A client that keeps sending requests can hold back every delivery and receipt, and
  nothing limits how many DMs a device may send.
- **No history in the database for the worker.** Reconnects, drifted settings and account switches are only in the
  worker's log; the panel shows the current state and the last error.
- **A double click can run a command twice.** The buttons are disabled while their request is in flight, but a double
  submit that gets through creates two node commands, such as two adverts or a second reboot. They run one after the
  other and expire if the worker is away.
- **The relay advertises only when asked.** It sends adverts during pairing and when you press *Send advert*, never on
  a timer, so a node far away learns about it only from a flood advert.
- **Messages are kept, unencrypted, until the sender or the recipient is deleted.** Only the traffic log is pruned.
- **The server's clock is the node's clock.** The XIAO loses its clock on every reboot, and the worker sets it from the
  server's. It only ever moves the node's clock forward, so a server clock that runs ahead leaves the node ahead until
  its next reboot. The server needs a correct clock even off-grid.
