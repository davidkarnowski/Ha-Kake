<!--
SPDX-FileCopyrightText: 2026 David D. Karnowski
SPDX-License-Identifier: CC-BY-SA-4.0
-->

# Ha-Kake over MQTT — the wire protocol

This is a standalone specification. You can build a gauge, a logger or a
bridge from it without reading any of the project's Python.

**Status (2026-09-09): specified, implemented on both ends (`mqttsource.py`,
`bridge/hakake_bridge.py`) and tested in-process against fake clients and
python-can's virtual bus. Nothing has yet run against a real broker, a
Raspberry Pi, or a car.** Treat every number that describes the bus as a
design expectation until someone measures it.

## 1. Purpose and audiences

Two things travel over the broker, for two audiences:

1. **CAN frames and read requests** — for a Ha-Kake reader ingesting a car
   that is somewhere else. A Raspberry Pi on the car's OBD port (`bridge/`)
   mirrors raw CAN frames to per-id topics and runs the reader's ISO-TP read
   requests. The reader (`--adapter mqtt`) sees the same raw lines it would
   see from an ELM327, so its decoders, fixtures and profiles are unchanged.
   Anyone decoding CAN themselves (with `docs/SIGNALS.md`) can use these too.
2. **Decoded values** — for anybody with an MQTT client. Whenever a reader
   has `mqtt` configured, whichever adapter it uses (BLE, USB, native CAN or
   the bridge), it publishes its decoded record to `<prefix>/state` and every
   flat value to `<prefix>/signal/<key>`, retained. A gauge subscribes to one
   topic and gets a number.

If you only want numbers, read §2, §3.6–3.7, §7 and §8 and skip the rest.

## 2. Topic tree

`<prefix>` is chosen by whoever runs it (default `hakake/leaf`); `<bus>` is
`car` or `ev` — which physical bus the bridge is wired to, stated in its
config, never guessed. `<ID>` is a CAN id in ELM327 style: **3 upper-case hex
digits for 11-bit ids (`421`, `7BB`), 8 for 29-bit (`18DAF110`)**.

| Topic | Direction | QoS | Retained | Payload |
|---|---|---|---|---|
| `<prefix>/<bus>/rx/<ID>` | bridge → subscribers | 0 | no | **frame** (§3.1) |
| `<prefix>/<bus>/rx/_batch` | bridge → subscribers | 0 | no | **batch** (§3.2), opt-in on the bridge |
| `<prefix>/<bus>/tx/uds` | reader → bridge | 1 | no | **uds request** (§3.3) |
| `<prefix>/<bus>/tx/uds/<req>` | bridge → reader | 1 | no | **uds ack** (§3.4) |
| `<prefix>/<bus>/status` | bridge → subscribers | 1 | **yes** | **status** (§3.5); the Last Will sets `online: false` |
| `<prefix>/state` | reader → subscribers | 0 | **yes** | **state** (§3.6) — the whole decoded record |
| `<prefix>/signal/<key>` | reader → subscribers | 0 | **yes** | **signal** (§3.7) — one JSON scalar |
| `<prefix>/signal/<key>/<n>` | reader → subscribers | 0 | **yes** | one element of a list value (`cells/0` … `cells/95`) |

`rx/#` mirrors a whole bus. A bridge publishes *either* per-id frames *or*
batches, never both, so a subscriber never sees a frame twice. `state` and
`signal` live under `<prefix>` directly, not under `<bus>`: a decoded value
belongs to the car, not to a wire.

All payloads are UTF-8 JSON. Unknown keys must be ignored by every consumer;
producers may add keys without a version bump (§6).

## 3. Payloads

Each schema below is JSON Schema draft 2020-12.

### 3.1 frame — `rx/<ID>`

```json
{"t": 1757440000.123456, "id": "421", "d": "08 00 00"}
```

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "hakake/mqtt/frame",
  "type": "object",
  "required": ["t", "id", "d"],
  "properties": {
    "t":   {"type": "number", "description": "bridge clock, Unix epoch seconds, from the SocketCAN driver timestamp"},
    "id":  {"type": "string", "pattern": "^[0-9A-F]{3}$|^[0-9A-F]{8}$", "description": "CAN id, ELM style"},
    "d":   {"type": "string", "pattern": "^([0-9A-F]{2}( [0-9A-F]{2})*)?$", "description": "data bytes, upper-case hex, space separated, DLC preserved (0-8 bytes)"},
    "err": {"const": true, "description": "present only on a CAN error frame"},
    "ext": {"const": true, "description": "present only on a 29-bit id"}
  },
  "additionalProperties": true
}
```

**`id` + space + `d` is exactly the line an ELM327 prints with headers on**
(`421 08 00 00`). That is the contract with the decoders and the replay
fixtures: a frame received over MQTT and one captured over USB are the same
string. Data is never padded — a 3-byte frame has three bytes.

### 3.2 batch — `rx/_batch`

A window of frames (50 ms by default) in one message, for whole-bus mirroring
on a slow link or a small Pi.

```json
{"t0": 1757440000.100000, "frames": [[0, "1DB", "07 E6 00 00 00 00 00 00"], [12, "421", "08 00 00"], [25, "18DAF110", "01", {"ext": true}]]}
```

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "hakake/mqtt/batch",
  "type": "object",
  "required": ["t0", "frames"],
  "properties": {
    "t0": {"type": "number", "description": "epoch seconds of the window start"},
    "frames": {
      "type": "array",
      "items": {
        "type": "array",
        "prefixItems": [
          {"type": "integer", "minimum": 0, "description": "offset from t0 in milliseconds"},
          {"type": "string", "pattern": "^[0-9A-F]{3}$|^[0-9A-F]{8}$"},
          {"type": "string", "pattern": "^([0-9A-F]{2}( [0-9A-F]{2})*)?$"},
          {"type": "object", "properties": {"err": {"const": true}, "ext": {"const": true}}}
        ],
        "minItems": 3, "maxItems": 4
      }
    }
  }
}
```

A frame's absolute time is `t0 + dt_ms / 1000`. The fourth element is present
only when a flag is set.

### 3.3 uds request — `tx/uds`

One ISO-TP request. The reader publishes it; the bridge sends it on the bus,
handles flow control locally (the flow-control frame has to go out within
about a second of the first response frame — a WAN round trip must not be in
that path), and acks. **The response frames are not in the ack**: they arrive
on `rx/<rx>` like any other frame, and the reader captures them during the
request window.

```json
{"req": "a1b2", "tx": "79B", "rx": "7BB", "data": "21 01", "bs": 0, "stmin": 0, "timeout": 2.0}
```

The bridge sends a request only on a `tx`/`rx` pair listed in its own
`uds_targets` (`bridge/README.md`); any other pair is acked `refused` before a
frame is built.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "hakake/mqtt/uds-request",
  "type": "object",
  "required": ["req", "tx", "rx", "data"],
  "properties": {
    "req":     {"type": "string", "pattern": "^[0-9a-zA-Z_-]{1,32}$", "description": "request id chosen by the sender; the ack comes on tx/uds/<req>"},
    "tx":      {"type": "string", "pattern": "^[0-9A-Fa-f]{3}$|^[0-9A-Fa-f]{8}$", "description": "id the request is sent on (the ECU's request id)"},
    "rx":      {"type": "string", "pattern": "^[0-9A-Fa-f]{3}$|^[0-9A-Fa-f]{8}$", "description": "id the answer comes back on"},
    "data":    {"type": "string", "pattern": "^[0-9A-Fa-f]{2}( [0-9A-Fa-f]{2})*$", "description": "the service byte and its parameters; the first byte MUST be 21, 01, 03 or 07"},
    "bs":      {"type": "integer", "minimum": 0, "maximum": 255, "default": 0, "description": "ISO 15765-2 block size we ask the ECU for (0 = no limit)"},
    "stmin":   {"type": "integer", "minimum": 0, "maximum": 255, "default": 0, "description": "ISO 15765-2 STmin byte we ask the ECU for: 0-127 = milliseconds between consecutive frames"},
    "timeout": {"type": "number", "minimum": 0.1, "default": 2.0, "description": "seconds the bridge waits for the complete answer (it caps this at its own uds_timeout)"}
  }
}
```

`req` is chosen by the sender — Ha-Kake uses four random hex digits — and only
has to be unique among that sender's in-flight requests. It is part of a
topic, so it must not contain `/`, `+` or `#`. The pattern above is a rule for
producers: the bridge enforces only that `req` is non-empty and free of those
three characters, and echoes anything else back on the ack topic unchanged.

### 3.4 uds ack — `tx/uds/<req>`

```json
{"req": "a1b2", "ok": true, "frames": 29, "bytes": 194}
{"req": "a1b2", "ok": false, "error": "timeout", "frames": 0}
{"req": "a1b2", "ok": false, "error": "refused", "reason": "service not in the read-only set"}
{"req": "a1b2", "ok": false, "error": "busy", "reason": "request queue full"}
```

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "hakake/mqtt/uds-ack",
  "type": "object",
  "required": ["req", "ok"],
  "properties": {
    "req":    {"type": "string"},
    "ok":     {"type": "boolean"},
    "frames": {"type": "integer", "minimum": 0, "description": "how many rx frames the bridge saw for this request; a consumer can wait until it has received that many on rx/<rx>"},
    "bytes":  {"type": "integer", "minimum": 0, "description": "length of the reassembled ISO-TP payload, when ok"},
    "error":  {"type": "string", "enum": ["timeout", "bus-off", "refused", "error", "busy", "expired", "offline"], "description": "timeout: no complete answer within timeout; bus-off: the CAN interface failed; refused: the read-only rule (§5), a tx/rx pair not in the bridge's uds_targets, or listen-only; error: malformed request or ISO-TP protocol error (see reason); busy: the bridge's request queue (32) is full; expired: the request waited longer than its own timeout and was not sent; offline: produced by the reader itself when it has no broker"},
    "reason": {"type": "string"}
  }
}
```

Because `rx/<ID>` is QoS 0 and the ack QoS 1, the ack can overtake the last
response frame. A consumer should count frames of `rx` against `frames` for
a short grace (Ha-Kake waits up to 0.25 s) before closing its capture window.
A bridge in batch mode flushes its pending batch before publishing an ack.

### 3.5 status — `status` (retained)

Published every 2 s by the bridge; retained, so a subscriber learns the
bridge's state the moment it connects. The bridge's MQTT Last Will is the
same topic with `{"v": 1, "online": false, "bus": "car", "bridge": "…"}`, so
an unplugged Pi or a lost WiFi link shows as offline within the keep-alive
(30 s) without any action on the bridge's part.

```json
{"v": 1, "online": true, "bus": "car", "bitrate": 500000, "listen_only": false,
 "fps": 188.5, "fps_out": 188.5, "dropped": 0, "queue": 0, "errors": 0, "refused": 0,
 "adapter": "socketcan can0", "bridge": "hakake-bridge 0.1",
 "filter": ["284", "292", "355", "358", "385", "421", "5A9", "5B3", "5C5", "60D", "764", "7BB"],
 "batch_ms": 0, "t": 1757440000.0}
```

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "hakake/mqtt/status",
  "type": "object",
  "required": ["v", "online", "bus"],
  "properties": {
    "v":           {"type": "integer", "const": 1, "description": "protocol version (§6)"},
    "online":      {"type": "boolean"},
    "bus":         {"type": "string", "enum": ["car", "ev"]},
    "bitrate":     {"type": "integer"},
    "listen_only": {"type": "boolean", "description": "true: every tx/uds is refused; the interface may also be listen-only in the kernel"},
    "fps":         {"type": "number", "description": "frames/s received from the bus over the last status period"},
    "fps_out":     {"type": "number", "description": "frames/s handed to the broker"},
    "dropped":     {"type": "integer", "description": "frames dropped since start because the publish queue was full (WiFi/broker stall)"},
    "queue":       {"type": "integer", "description": "publish queue depth right now"},
    "errors":      {"type": "integer", "description": "CAN error frames and bus errors since start"},
    "refused":     {"type": "integer", "description": "tx/uds requests refused since start (§5)"},
    "adapter":     {"type": "string", "description": "python-can interface and channel"},
    "bridge":      {"type": "string", "description": "software name and version, e.g. \"hakake-bridge 0.1\""},
    "filter":      {"oneOf": [{"const": "all"}, {"type": "array", "items": {"type": "string"}}], "description": "the ids the bridge mirrors"},
    "batch_ms":    {"type": "integer", "description": "0 = one message per frame"},
    "t":           {"type": "number", "description": "bridge clock, epoch seconds"}
  }
}
```

### 3.6 state — `<prefix>/state` (retained)

The reader's whole decoded record, once per polling cycle (typically every
1–2 s over an ELM327, ~0.35 s over a native CAN adapter), plus on every status change (connecting, asleep, paused). It is the
same JSON the dashboard's `/api/status` serves, with `"v": 1` added.

The record is **open-ended by design**: its keys are whatever the active
vehicle profile decodes, plus the reader's bookkeeping. The schema therefore
fixes only what every record carries; everything else is described by the
profile's signal registry (§8).

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "hakake/mqtt/state",
  "type": "object",
  "required": ["v", "status", "state_time"],
  "properties": {
    "v":            {"type": "integer", "const": 1},
    "status":       {"type": "string", "enum": ["ok", "connecting", "reconnecting", "asleep", "paused", "stopped"], "description": "the reader's state; values other than ok mean the numbers are the last good ones"},
    "state_time":   {"type": "string", "format": "date-time", "description": "when this record was published (UTC)"},
    "timestamp":    {"type": "string", "format": "date-time", "description": "when the last successful poll completed (UTC); absent before the first"},
    "message":      {"type": "string", "description": "human-readable detail for a non-ok status"},
    "adapter_type": {"type": "string", "enum": ["ble", "usb", "can", "mqtt", "replay", "sim"]},
    "adapter_name": {"type": "string"},
    "replay":       {"type": "boolean", "description": "true: a recorded fixture, NOT a live car"},
    "simulated":    {"type": "boolean", "description": "true: a running model, NOT a car"},
    "remote":       {"type": "object", "properties": {"host": {"type": "string"}, "bus": {"type": "string"}}, "description": "present when the car is read through a bridge"},
    "cycle_s":      {"type": "number", "description": "how long the last poll took, seconds to the millisecond"},
    "item_age":     {"type": "object", "additionalProperties": {"type": "number"}, "description": "seconds since each polled item was last read — the staleness of every value (ms precision)"},
    "item_dur":     {"type": "object", "additionalProperties": {"type": "number"}, "description": "seconds each item's last read took (ms precision); live only, never stored"},
    "item_gap":     {"type": "object", "additionalProperties": {"type": "number"}, "description": "seconds between each item's last two reads — its real refresh period (ms precision); live only, never stored"},
    "items":        {"type": "array", "items": {"type": "string"}, "description": "the items being polled (driven by the dashboard's enabled tiles)"},
    "cells":        {"type": "array", "items": {"type": "number"}, "description": "per-cell-pair voltages when the profile reads them"}
  },
  "additionalProperties": true,
  "description": "Every other key is a decoded signal: see docs/SIGNALS.md and the profile's SIGNALS registry for its meaning and unit. Temperatures come in pairs, <name>_c and <name>_f. Currents are negative when discharging; power follows."
}
```

`state` is the reader's record as it stands, bookkeeping included — the
adapter's name and port (`adapter_port` is a serial device path or a BLE
address) travel with it. That is fine on the car's LAN; it is one more
reason a broker is never exposed beyond it (§5).

A gauge that reads `state` should honour `status` and `replay`/`simulated`:
a record with `status: "asleep"` still carries the last numbers, and a
`replay: true` record is a recording, not a car.

### 3.7 signal — `<prefix>/signal/<key>` (retained)

One value, as a bare JSON scalar — the whole payload is `87.5`, `true` or
`"P"`. Nothing to parse for a number; `JSON.parse` for the general case.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "hakake/mqtt/signal",
  "type": ["number", "boolean", "string"],
  "description": "the value of record[key]; a list value publishes one topic per index, signal/<key>/<n>"
}
```

Which keys exist is the profile's business (§8); `<key>` is the record key
verbatim (`soc`, `pack_v`, `hv_current_a`, `ambient_f` …), and a list such as
`cells` or `temps_f` publishes `signal/cells/0` … `signal/cells/95`. Objects
(`item_age`, `timing`), `null` values and lists of non-scalars are **not**
published as signals — they live in `state`. Nor are keys that begin with `_`
or that contain a `/`, since a key is a topic segment.

Signals are published **on change** and retained; `state` is published every
cycle. So a signal topic always holds the current value for a new subscriber,
but it will not "tick" while the value is stable — for a heartbeat, watch
`state` (its `state_time` changes every cycle) or the bridge's `status`.

## 4. Field semantics, in one place

| Field | Meaning |
|---|---|
| `t`, `t0` | The **bridge's** clock, Unix epoch seconds, taken from the SocketCAN driver timestamp when it has one. Do not compare it with the reader's clock unless both machines run NTP. Fixtures made from a stream convert it to offsets from the first frame — a timestamp identifies the day a car was driven. |
| `id` | ELM style, upper-case, 3 or 8 hex digits. Topic and payload carry the same value; the payload is authoritative. |
| `d` | Upper-case hex pairs separated by single spaces; `""` for a zero-length frame. DLC preserved. |
| `err` | The kernel reported a CAN error frame. Consumers usually count and drop it. |
| `ext` | 29-bit id. Also implied by an 8-digit `id`. |
| `bs`, `stmin` | The flow-control parameters the bridge sends to the ECU on the requester's behalf — the same numbers an ELM327 takes from `ATFCSD 30 <bs> <stmin>`. `stmin` is the raw STmin byte (0–127 ms). |
| `v` | Protocol version, §6. |

## 5. Read-only, enforced on both ends

Ha-Kake never writes to a car (`SECURITY.md`). Over MQTT that promise has two
guards, and neither trusts the other:

- **The bridge** — the only process that can actually transmit — accepts a
  `tx/uds` request only when the first byte of `data` is one of
  **`0x21`** (UDS ReadDataByLocalIdentifier), **`0x01`**, **`0x03`**, **`0x07`**
  (OBD-II live data, stored and pending trouble codes). Anything else —
  `0x04` clear codes, `0x2E`/`0x3D` write, `0x27` security access,
  `0x31` routine control, `0x10` session control, `0x22`, `0x1A` — is
  answered `{"ok": false, "error": "refused"}`, logged, counted in
  `status.refused`, and never reaches the bus. With `--listen-only` every
  request is refused, and the kernel interface should be brought up
  `listen-only on` as well.
- **The reader** refuses the same set before publishing, so a refusal is
  normally never even seen on the wire.

The whitelist is the whole allowed set; widening it is a change to
`SECURITY.md` and a documented review, not a config option. What the list
does *not* do is make a broker safe to expose: whoever can publish to
`tx/uds` can keep ECUs awake by polling. The broker runs plain MQTT — no
credentials, no TLS, by design — so it lives on the car's LAN only, and beyond
the LAN the path is a **Tailscale tailnet or a private VPN**, which
authenticates and encrypts end to end with nothing to configure here
(`bridge/README.md`). Never expose it to the internet directly.

## 6. Versioning

`status` and `state` carry `"v": 1`. Rules:

- Adding a key anywhere, adding a topic, adding an `error` value: **no bump**.
  Consumers ignore what they do not know.
- Changing the meaning or type of an existing key, renaming a topic, changing
  the id or data format: **bump `v`**, and publish the new protocol under a
  new `<prefix>` for a transition period so old gauges keep working on the
  old one. A consumer that sees a `v` it does not understand should say so
  rather than guess.

`frame`, `batch` and the UDS envelopes carry no `v` (a version field on
1,700 messages a second buys nothing); they are versioned by the `status`
topic of the bridge that publishes them.

## 7. Worked examples

`mosquitto_sub`/`mosquitto_pub` ship with Mosquitto (`apt install
mosquitto-clients`, `brew install mosquitto`). `<broker>` is your Pi or
wherever the broker runs — plain MQTT, so nothing else to pass.

```bash
# Is the bridge alive? Retained: you get an answer immediately, even if it is offline.
mosquitto_sub -h <broker> -t 'hakake/leaf/car/status' -v
# hakake/leaf/car/status {"v":1,"online":true,"bus":"car","bitrate":500000,...}

# Watch one CAN id (the Leaf's 0x421 carries the gear lever):
mosquitto_sub -h <broker> -t 'hakake/leaf/car/rx/421' -v
# hakake/leaf/car/rx/421 {"t":1757440000.123456,"id":"421","d":"08 00 00"}

# Every frame on the bus (or the batches, if the bridge bundles):
mosquitto_sub -h <broker> -t 'hakake/leaf/car/rx/#' -v

# Record a drive for later replay (docs/REPLAY.md): one JSON object per line
mosquitto_sub -h <broker> -t 'hakake/leaf/car/#' -F '{"topic":"%t","payload":%p}' > drive.jsonl
python record_session.py --from-mqtt drive.jsonl --vehicle leaf_ze0 --out drive.json

# Ask the Leaf's battery controller for group 01 (a read; anything else is refused):
mosquitto_sub -h <broker> -t 'hakake/leaf/car/tx/uds/+' -t 'hakake/leaf/car/rx/7BB' -v &
mosquitto_pub -h <broker> -t 'hakake/leaf/car/tx/uds' -q 1 \
  -m '{"req":"demo1","tx":"79B","rx":"7BB","data":"21 01","bs":0,"stmin":0,"timeout":2.0}'
# hakake/leaf/car/rx/7BB {"t":...,"id":"7BB","d":"10 29 61 01 FF FF F9 E9"}
# hakake/leaf/car/rx/7BB {"t":...,"id":"7BB","d":"21 02 87 FF FF FC 44 FF"}
# ...
# hakake/leaf/car/tx/uds/demo1 {"req":"demo1","ok":true,"frames":6,"bytes":41}

# What a refusal looks like (mode 04 clears trouble codes; it never leaves the bridge):
mosquitto_pub -h <broker> -t 'hakake/leaf/car/tx/uds' -q 1 \
  -m '{"req":"nope","tx":"7E0","rx":"7E8","data":"04"}'
# hakake/leaf/car/tx/uds/nope {"req":"nope","ok":false,"error":"refused","reason":"service not in the read-only set"}

# The decoded car, whole record (retained; the last cycle appears at once):
mosquitto_sub -h <broker> -t 'hakake/leaf/state'

# One number:
mosquitto_sub -h <broker> -t 'hakake/leaf/signal/soc'
# 87.5

# Every decoded value, one per line, as they change:
mosquitto_sub -h <broker> -t 'hakake/leaf/signal/#' -v
# hakake/leaf/signal/soc 87.5
# hakake/leaf/signal/pack_v 393.2
# hakake/leaf/signal/hv_current_a -1.25
# hakake/leaf/signal/gear "P"
# hakake/leaf/signal/cells/0 3.95
```

### 7.1 A Python gauge (paho-mqtt 2.x)

```python
#!/usr/bin/env python3
# pip install paho-mqtt   — prints the state of charge whenever it changes.
import json, sys
import paho.mqtt.client as mqtt

BROKER, PREFIX, KEY = sys.argv[1], "hakake/leaf", "soc"

def on_connect(client, userdata, flags, reason_code, properties):
    client.subscribe(f"{PREFIX}/signal/{KEY}")          # retained: the last value arrives at once

def on_message(client, userdata, msg):
    value = json.loads(msg.payload)                     # a bare JSON scalar: 87.5
    stale = " (retained)" if msg.retain else ""
    print(f"{KEY} = {value}{stale}")

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect, client.on_message = on_connect, on_message
client.connect(BROKER, 1883, keepalive=30)
client.loop_forever()
```

Run it: `python gauge.py <broker>`. Subscribe to `<prefix>/state` instead and
`json.loads` the whole record when you want several values, or the
`item_age`/`status` bookkeeping.

### 7.2 A browser gauge (MQTT over WebSockets)

Needs a WebSocket listener on the broker (`listener 9001` + `protocol
websockets` in Mosquitto, LAN-only). The client library is MQTT.js from a
CDN; pin the version you tested.

```html
<p>State of charge: <b id="soc">–</b> %</p>
<script src="https://cdnjs.cloudflare.com/ajax/libs/mqtt/5.10.1/mqtt.min.js"></script>
<script>
  const client = mqtt.connect("ws://BROKER:9001");        // plain MQTT over WebSockets, LAN or tailnet
  client.on("connect", () => client.subscribe("hakake/leaf/signal/soc"));
  client.on("message", (topic, payload) => {
    document.getElementById("soc").textContent = JSON.parse(payload.toString());
  });
</script>
```

### 7.3 What to do with retained messages

`status`, `state` and every `signal/*` topic are retained. On connect you
receive the last value of each **immediately**, flagged `retain` by the
client library. That is the point — a gauge shows something the moment it
opens — but it can be old: a reader that stopped an hour ago left its last
record retained. So:

- Show the value, and show its age: `state.state_time` (and `item_age` per
  value) tells you how old it is; the bridge's `status.online` tells you
  whether there is a car to read at all.
- Treat a retained `signal/*` value as "last known", not "now", until a
  non-retained update follows.
- A publisher that wants to remove a stale value publishes an empty retained
  payload to the topic; Ha-Kake does not do this for signals (values are
  sticky in the reader's cache by design), so age is the thing to watch.

## 8. What the values mean

The protocol carries keys; the meaning lives elsewhere, on purpose:

- **`docs/SIGNALS.md`** — the authority on what each CAN byte means and how
  sure the project is (verified in a car vs tentative). If you decode
  `rx/<ID>` frames yourself, start there.
- **The signal registry** — `signals.py` and each profile's `SIGNALS` table
  (`vehicles/<profile>.py`) list every record key with its label, unit,
  range and colour scale; the dashboard's `/api/signals` serves it as JSON.
  The keys there are the `<key>` in `signal/<key>`.

Conventions that hold across all profiles: temperatures come in pairs,
`<name>_c` and `<name>_f`, both published; current is **negative when
discharging** and power follows the same sign; `soc` and `soh` are percent.

## 9. Throughput

From the project's frame-rate survey of a 2012 Leaf (Car-CAN ≈ 1,690 frames/s
in total, EV-CAN ≈ 790), with ≈ 60 B of JSON + ≈ 10 B of MQTT framing per
frame:

| Subscription | frames/s | ≈ bytes/s |
|---|---|---|
| The 11 passive ids the Leaf reader polls today | ≈ 190 | 15 kB/s |
| + pedals / steering / torque / power ids | ≈ 550 | 40 kB/s |
| + one 100 Hz id for a high-rate buffer | ≈ 650 | 50 kB/s |
| Whole Car-CAN (`rx/#`) | ≈ 1,700 | 120 kB/s |
| Both buses | ≈ 2,500 | 180 kB/s |

Mosquitto on a Pi 4 handles tens of thousands of messages a second; a Python
subscriber receives a few thousand comfortably. Per-id subscriptions are
therefore free, a whole-bus mirror is fine on Ethernet and marginal over LTE,
and batches (§3.2) exist for that case. The in-car WiFi link is the weak
point, not the software — and the bridge drops (and counts) frames rather than
buffering without bound when it stalls. `state`/`signal` traffic is
negligible: one record plus the changed signals per cycle.

## 10. Running it

**Reader side** (`config.local.json`, gitignored; `HAKAKE_MQTT_HOST`,
`HAKAKE_MQTT_PORT`, `HAKAKE_MQTT_PREFIX` and `HAKAKE_MQTT_BUS` override it —
`subscribe` and `batch` have no environment override):

```json
{"mqtt": {"host": "", "port": 1883, "prefix": "hakake/leaf", "bus": "car",
          "subscribe": "auto", "batch": false}}
```

- With `host` set, **any** reader publishes `state` and `signal/*`.
- `python web/app.py --adapter mqtt` reads the car *through* the bridge. It is
  never auto-detected: a wrong broker is a wrong car. The record carries
  `adapter_type: "mqtt"` and `remote: {host, bus}`.
- `subscribe: "auto"` subscribes to the ids the enabled tiles actually use
  (learned from the profile's `ATCRA` commands, growing as tiles are enabled;
  `rx/#` until the first one). A list of ids or full topics pins it instead.
  `batch: true` subscribes to `rx/_batch` (whole bus) instead of per-id topics.

- **A bridge per bus, or one bridge with two subtrees** (2026-09-09): the
  reader's `adapters` list names an MQTT entry per bus — `{"type": "mqtt",
  "bus": "ev", "host": "…"}` beside a `usb` or `can` entry for Car-CAN, or
  two `mqtt` entries pointing at `<prefix>/car/` and `<prefix>/ev/` on one
  broker. An entry's `host` / `port` / `prefix` override the `mqtt` block
  for that bus only. Both buses are polled concurrently and each reconnects
  on its own (`docs/ARCHITECTURE.md` "Several adapters"). In-process tests
  only so far.

**Bridge side**: `bridge/README.md` — install on a Raspberry Pi, `ip link`
bring-up, Mosquitto on the LAN (Tailscale or a VPN beyond it), the systemd unit, sizing for a Pi
Zero 2 W.

**Reader hook**: the reader calls `mqttsource.publish_state(record)` once per
cycle; it is a no-op without an `mqtt.host` and never raises, so a broker
outage cannot stop the reader.
