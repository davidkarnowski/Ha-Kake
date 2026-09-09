<!--
SPDX-FileCopyrightText: 2026 David D. Karnowski
SPDX-License-Identifier: CC-BY-SA-4.0
-->

# hakake-bridge — the car's CAN bus, over MQTT, from a Raspberry Pi

A small Python process that sits on the car's diagnostic connector and mirrors
CAN frames to an MQTT broker, and runs the reader's ISO-TP **read** requests on
its behalf. The wire protocol is `docs/MQTT.md` in the main repository; a
Ha-Kake reader ingests it with `--adapter mqtt`, and any MQTT client can build
a gauge on the same stream.

**Status (2026-09-09): written and tested against python-can's `virtual` bus
and a fake broker. It has not yet run on a Pi, against a real broker, or on a
car.** Hardware selection and firmware notes are pending the owner's bench
check; see `docs/CAN_TRANSPORT.md` when it lands.

## What it does, in one screen

```
 car ──CAN──▶ can0 (kernel) ──filter──▶ hakake_bridge.py ──▶ mosquitto ──▶ reader / gauges
                                             ▲                     │
                                             └── tx/uds (reads) ◀──┘
```

- Reads `can0` through SocketCAN with python-can. Frames become
  `<prefix>/<bus>/rx/<ID>` messages (`{"t": …, "id": "421", "d": "08 00 00"}`),
  or 50 ms bundles on `rx/_batch` when `batch_ms` is set.
- Publishes a retained `status` every 2 s (rates, drops, queue depth, errors,
  filter, listen-only) with a Last Will that flips it to `online: false`.
- Consumes `tx/uds` requests, runs them through `can-isotp` (userspace, so the
  answer's raw frames are mirrored like every other frame) and acks on
  `tx/uds/<req>`.
- **Refuses anything that is not a read.** The service byte must be one of
  `0x21`, `0x01`, `0x03`, `0x07` — `SECURITY.md`'s whole allowed set. Mode
  `0x04` (clear codes), `0x2E`, `0x27`, `0x31`, `0x10` and everything else are
  acked `"refused"` and logged. `--listen-only` refuses every request. The
  reader refuses the same set before publishing; the bridge does not rely on
  that, because the bridge is the process that can actually transmit.

## Sizing and architecture — built for a Pi Zero 2 W

These are **expectations, not measurements** — nobody has run this on a Pi yet.
Numbers for the bus come from the memo's frame-rate table (Car-CAN ≈ 1,700
frames/s in total, EV-CAN ≈ 800).

| Setup | Frames/s to Python | Zero 2 W | Pi 4 |
|---|---|---|---|
| `ids` list with the reader's passive + response ids (the example config) | ≈ 200–600 | comfortable | trivial |
| Whole bus, `batch_ms: 50` | ≈ 1,700 in, ≈ 20 msgs/s out | expected fine | fine |
| Whole bus, one message per frame | ≈ 1,700 in and out | marginal — use batch | fine |
| Both buses (two bridges or two interfaces), per frame | ≈ 2,500 | no | expected fine |

What makes the Zero viable:

1. **SocketCAN is the input.** A CANable running candleLight firmware appears
   as `can0` through the kernel's `gs_usb` driver; an MCP2515 SPI HAT appears
   as `can0` through `mcp251x`. Either way the kernel does the framing and the
   bridge reads whole frames. `slcan` (the text protocol over a serial port) is
   supported as a fallback (`"interface": "slcan"`, `"channel": "/dev/ttyACM0"`)
   but python-can parses it **one byte per syscall** — at 1,700 frames/s that
   is ~40,000 syscalls/s, a large slice of one A53 core. Prefer a kernel
   `can0`. Which hardware, and how to get its firmware there, is pending the
   owner's bench check (`docs/CAN_TRANSPORT.md`).
2. **Kernel-level filtering.** `ids` becomes a `CAN_RAW_FILTER` on the socket
   (`bus.set_filters`), so frames nobody asked for never leave the kernel. A
   request's response id is added to the filter automatically the first time it
   is used (logged), so `ids` need not anticipate every UDS target — but
   listing them (`7BB`, `764` on the example config) avoids a first-cycle miss.
   `"ids": "all"` mirrors the bus.
3. **Cheap serialisation.** The topic string is computed once per id; `d` is
   built from a 256-entry hex table; the frame message is a hand-built string
   of the fixed schema rather than `json.dumps` (measured on the development
   laptop: 0.33 µs vs 1.18 µs per frame, 3.5×; the hex table 0.15 µs vs 0.78 µs
   for 8 bytes). The output is byte-for-byte what `json.dumps` would produce.
4. **Batch mode** (`batch_ms: 50`) bundles a window of frames into one message
   with millisecond offsets — ~20 messages/s instead of ~1,700 on a whole-bus
   mirror. A pending batch is always flushed before a request's ack, so a
   reader waiting on the ack never misses the last response frames. A bridge
   publishes *either* per-id frames *or* batches, never both.
5. **Backpressure.** The publish queue is bounded (`queue`, default 2,000
   items). When WiFi or the broker stalls, the oldest items are dropped and
   counted — `dropped` and `queue` are in every `status` message and in the
   `--stats` line — and memory never grows. Frames lost this way are gone;
   that is the honest behaviour on a 512 MB device.
6. **Headless.** A systemd unit with `Restart=always`, `After=network-online.target`,
   the `can0` bring-up as `ExecStartPre`, output to journald, and a stats line
   every 10 s (`fps in/out, dropped, queue, errors, tx, refused`).

## Install on Raspberry Pi OS

Everything below is generic SocketCAN and Python; nothing is board-specific.

```bash
sudo apt install -y python3-venv mosquitto mosquitto-clients can-utils
sudo mkdir -p /opt/hakake-bridge && sudo chown "$USER" /opt/hakake-bridge
cd /opt/hakake-bridge
python3 -m venv venv
./venv/bin/pip install python-can==4.6.1 can-isotp==2.0.7 paho-mqtt==2.1.0
# copy hakake_bridge.py, config.example.json → config.json, hakake-bridge.service here
```

Bring the interface up and look at the bus before starting anything:

```bash
sudo ip link set can0 up type can bitrate 500000 restart-ms 100
candump -n 20 can0            # frames should scroll; if not, check wiring and bitrate
```

For **EV-CAN**, or any bus you want to be *certain* nothing is ever sent on,
let the kernel refuse too:

```bash
sudo ip link set can0 up type can bitrate 500000 listen-only on restart-ms 100
```

and run the bridge with `--listen-only` (or `"listen_only": true`): the kernel
drops transmissions, and the bridge acks every request `"refused"` without
trying. Belt and braces, on purpose. Which OBD pins carry which bus on your car
is in `docs/CAN_TRANSPORT.md`; the bridge never guesses — `bus` in the config
is your statement of what it is wired to, and it goes into every topic.

Run it by hand first:

```bash
./venv/bin/python hakake_bridge.py --config config.json --stats 5
```

Then as a service:

```bash
sudo cp hakake-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hakake-bridge
journalctl -u hakake-bridge -f
```

Check from any machine on the LAN:

```bash
mosquitto_sub -h <pi> -t 'hakake/leaf/car/status' -v          # retained: appears at once
mosquitto_sub -h <pi> -t 'hakake/leaf/car/rx/+' -v            # frames
```

### Configuration (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `host`, `port`, `tls`, `username`, `password` | `127.0.0.1`, `1883`, `false`, `""`, `""` | the broker; a broker on the Pi itself needs nothing else |
| `prefix` | `hakake/leaf` | the topic root shared with the reader's `mqtt.prefix` |
| `bus` | `car` | `car` or `ev` — which bus this Pi is wired to; part of every topic |
| `interface`, `channel` | `socketcan`, `can0` | python-can interface and channel; `slcan` + `/dev/ttyACM0` as a slow fallback |
| `bitrate` | `500000` | informational for `socketcan` (the `ip link` line sets it); used for `slcan` |
| `listen_only` | `false` | refuse every `tx/uds`; also set `listen-only on` on the interface |
| `ids` | `all` | list of hex ids to mirror (kernel filter), or `"all"` |
| `batch_ms` | `0` | `0` = one message per frame; `50` = bundles on `rx/_batch` |
| `status_s`, `stats_s` | `2.0`, `10.0` | status period; stats-line period (`0` = off) |
| `queue` | `2000` | publish queue depth before the oldest is dropped |
| `uds_timeout` | `5.0` | cap on a request's own `timeout` |

The example `ids` list is what a 2011–2012 Leaf reader polls today (the
passive ids in its profile plus the LBC and HVAC response ids) — an *example*
for that profile, not a default of the bridge. Every command-line flag
(`--host`, `--bus`, `--ids 421,358`, `--batch-ms 50`, `--listen-only`,
`--stats 10`) overrides the file.

## Mosquitto and security

The bridge is a second process on a safety-relevant bus, and the broker is the
only door to it. Keep it on the car's LAN or behind a VPN, never on the
internet. A minimal `/etc/mosquitto/conf.d/hakake.conf`:

```
listener 1883
allow_anonymous false
password_file /etc/mosquitto/passwd
```

```bash
sudo mosquitto_passwd -c /etc/mosquitto/passwd hakake     # prompts for a password
sudo systemctl restart mosquitto
```

Put the same username and password in the bridge's `config.json` and in the
reader's `config.local.json` (`mqtt.username` / `mqtt.password`). Both files
are machine-local: the reader's is gitignored, and the Pi's lives only on the
Pi. For a WebSocket listener (browser gauges), add `listener 9001` and
`protocol websockets` to the same file — still LAN-only.

What the whitelist protects against is a *well-meaning* client sending
something other than a read. It does not make the broker safe to expose:
anyone who can publish to `tx/uds` can keep ECUs awake by polling, which on a
first-generation Leaf is a flat 12 V battery. Credentials and a private
network are the actual protection.

## Testing without a Pi

`tests/test_bridge.py` in the main repository runs this file against
python-can's `virtual` interface with a fake ECU answering ISO-TP and a fake
broker in process: `pytest -q tests/test_bridge.py`. It proves the JSON
shapes, the filter, batching, the round trip, the refusals and the LWT. It
proves nothing about a Pi, a CAN transceiver or a car — see the Status line.
