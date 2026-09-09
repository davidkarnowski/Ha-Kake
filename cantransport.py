#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Native CAN transport — an ELM327-speaking façade over a frame source.

The reader, the vehicle profiles and the decoders all talk ELM327: `ATSH`,
`ATCRA`, `ATMA`, `2101`. A native CAN controller (a CANable 2.0 class board
on USB, or a bridge on the far side of an MQTT broker) speaks frames. This
module is the adapter between the two, so that nothing above `send()` has to
change — the precedent is `ReplayELM` / `SimELM` in elm327.py, which keep
"exactly the adapter state a real ELM327 keeps between commands" and answer
the rest from somewhere other than a car.

    reader ──ELM──▶ CanFacade ──frames──▶ FrameSource ──▶ LocalSource (python-can, USB)
                                                     └──▶ MqttSource  (mqttsource.py)

Classes:
  FrameSource   — the contract a source implements (frames in, one UDS request out)
  LocalSource   — python-can: slcan / gs_usb (CANable), socketcan, virtual (tests)
  CanFacade     — the ELM327 state machine: ATSH/ATCRA/ATCAF/ATFCSD/ATMA, UDS via
                  the source, `ATI` for the liveness probe, `marker()` for the record

Functions:
  open_can(cfg, log)   — what elm327.detect_adapter("can") calls
  can_config(cfg)      — config.local.json `can_*` keys, HAKAKE_CAN_* env wins
  classify_usb(...)    — which firmware a CANable runs, from USB VID:PID
  can_line(id, data)   — the one line format everything downstream parses

What the façade promises, and what it refuses:

  * Line shape is byte-identical to what SerialELM / BleELM return after they
    strip the echo, the prompt and `OK`: `421 08 00 00` (DLC preserved, never
    padded), and a UDS answer as the *raw* response frames in bus order —
    `7BB 10 29 61 01 …`, `7BB 21 …` — exactly `leaf_decoders.parse_isotp()`'s
    input. Fixtures recorded through any transport stay interchangeable.
  * `ATMA` answers from a table of recently received frames, using the
    caller's `timeout` as the window ("what arrived in the last `secs`") and
    drains what it returns. It returns at once: a native controller receives
    everything all the time, so there is no dwell — `PASSIVE_INSTANT`.
  * Read-only, at this layer too: a request whose service byte is not one of
    `0x21` / `0x01` / `0x03` / `0x07` answers `NO DATA` and is logged, and on a
    bus opened listen-only *every* non-AT command answers `NO DATA`. Neither is
    an exception — a misconfigured item must not put the supervisor into a
    reconnect loop.
  * `ATCAF0` / `ATCAF1` are bookkeeping only. There is no `DATA ERROR` here.
  * The façade knows CAN ids only as the strings the profile hands it. Nothing
    in this file names a vehicle.

Thread model: the source's thread (python-can's Notifier, or paho's network
loop) calls `on_frame` and writes the tables under `CanFacade.lock`; the
asyncio loop reads under the same lock. A UDS request runs in the default
executor. Nothing blocks the loop.

Nothing here has run on hardware yet (2026-09-09): the board had not arrived.
`docs/CAN_TRANSPORT.md` says what is verified by tests and what is modelled.
"""

import asyncio
import collections
import glob
import os
import sys
import threading
import time

import can
from can.interfaces.slcan import slcanBus as _SlcanBus      # pyserial is already a dependency

from util import env

# ── constants ────────────────────────────────────────────────────────────

# The read services this project sends (SECURITY.md). A request whose first
# byte is anything else never reaches a bus through this module.
READ_SERVICES = frozenset({0x21, 0x01, 0x03, 0x07})

BUSES = ("car", "ev")
DEFAULT_BITRATE = 500000

# USB VID:PID → firmware (memo §2.3). The board cannot say which pins it is
# wired to, only which firmware it runs; the bus is always configuration.
CANDLE_IDS = ((0x1D50, 0x606F), (0x1209, 0x2323))     # candleLight / gs_usb
SLCAN_IDS = ((0x16D0, 0x117E),)                        # normaldotcom/canable2-fw (stock)
DFU_IDS = ((0x0483, 0xDF11),)                          # STM32 bootloader: stuck in boot mode

DFU_ADVICE = ("adapter is in DFU/boot mode (USB 0483:df11) — unplug it for 5 s and plug it "
              "back in; if it keeps happening, see docs/CAN_TRANSPORT.md on BOOT0")

SLCAN_PORT_PATTERNS = ("/dev/cu.usbmodem*", "/dev/ttyACM*")


def can_line(id_hex, data):
    """`421 08 00 00` — the line the decoders, fixtures and record_session parse.

    DLC is preserved: the Leaf's 0x421 is three bytes and prints as three.
    A frame with no data prints as the bare id."""
    if not data:
        return id_hex
    return f"{id_hex} " + " ".join(f"{b:02X}" for b in data)


def id_hex(arbitration_id, extended=False):
    """ELM-style id: 3 hex digits for 11-bit, 8 for 29-bit (what ATH1 shows)."""
    return f"{arbitration_id:08X}" if extended else f"{arbitration_id:03X}"


# ── the source contract ──────────────────────────────────────────────────

class FrameSource:
    """What a frame source provides. `LocalSource` below and `MqttSource`
    (mqttsource.py) implement it; the façade owns the ELM state and the
    tables, a source only moves frames.

        async start(on_frame, on_status)   on_frame(t, id_hex, data: bytes, flags: dict)
                                           is called on the source's own thread for
                                           every frame; `t` is the source's clock
                                           (epoch seconds), `flags` is {} or carries
                                           "err" (error frame) / "ext" (29-bit id).
                                           on_status(dict) reports {"online": bool, ...}
                                           whenever that changes (an "error" key says why).
        async stop()
        async subscribe(ids | None)        None = everything. Local ignores it.
        async send_uds(tx, rx, data: bytes, bs, stmin, timeout) -> dict
                                           one ISO-TP request; the response frames
                                           arrive through on_frame like any other and
                                           the façade captures them. Returns the ack
                                           shape of the MQTT protocol (plan §3.2):
                                           {"ok": True, "frames": N} or
                                           {"ok": False, "error": "timeout|bus-off|refused"}.
        liveness() -> dict                 at least {"online", "fps", "t_last"}.
        listen_only: bool                  True → the source never transmits.
        bus: str                           "car" | "ev" — configuration, never guessed.
        name: str                          becomes adapter_name; must contain a digit
                                           (Reader.probe_alive demands one).
    """

    listen_only = False
    bus = "car"
    name = ""

    async def start(self, on_frame, on_status):
        raise NotImplementedError

    async def stop(self):
        pass

    async def subscribe(self, ids):
        pass

    async def send_uds(self, tx, rx, data, bs, stmin, timeout):
        return {"ok": False, "error": "refused"}

    def liveness(self):
        return {"online": False, "fps": 0, "t_last": 0.0}


# ── the façade ───────────────────────────────────────────────────────────

class CanFacade:
    """ELM327 look-alike over a FrameSource. See the module docstring."""

    adapter_type = "can"

    # Cost of one poll relative to BLE, for Reader.estimate(). At 500 kbit/s a
    # group-01 answer is ~2 ms of bus plus the ECU's turnaround; the USB ELM
    # chose 0.1 "deliberately several times more pessimistic" than its
    # measurements, and the same reasoning gives 0.05 here (memo §4.9).
    # Unmeasured: no hardware yet.
    SPEED = 0.05
    # ISO-TP separation time we ask an ECU for. The 0x20 the BLE link needs is
    # the ELM clone's limitation, not the ECU's; a native controller absorbs
    # consecutive frames at bus speed. Phase (b) verifies the LBC honours 0.
    STMIN = "00"
    # Passive items cost no dwell here: Reader.estimate() drops their `secs`.
    PASSIVE_INSTANT = True

    def __init__(self, source, stmin_override=None, settle=0.3, recent=64, log=print):
        self.source = source
        self.bus = source.bus
        self.listen_only = bool(source.listen_only)
        self.stmin_override = stmin_override      # int, or None = honour ATFCSD
        self.settle = settle                      # seconds to let the table fill after connect
        self._log = log
        self.adapter_name = source.name or ""
        self.adapter_port = getattr(source, "port", "") or ""
        # adapter state a real ELM327 keeps
        self.tx = None            # ATSH  — request header (which ECU we address)
        self.rx = None            # ATCRA — response filter
        self.caf = True           # ATCAF1 (ISO-TP formatting) vs ATCAF0 (raw) — bookkeeping only
        self.fc_tx = None         # ATFCSH — flow-control header (always == tx in this project)
        self.fc_bs = 0            # ATFCSD 30 <BS> <STmin>
        self.fc_stmin = 0
        self.fc_mode = 1          # ATFCSM
        self.commands = 0
        self.misses = []          # requests that answered NO DATA
        self.refused = []         # requests refused by policy (listen-only / not a read service)
        self.last_ack = None
        # the tables (memo §4.4)
        self.lock = threading.Lock()
        self.latest = {}                                   # id -> (mono_t, src_t, bytes)
        self.recent = collections.defaultdict(lambda: collections.deque(maxlen=recent))
        self._captures = {}                                # id -> list, during a request
        self.frames = 0
        self.errors = 0
        self.t_last = 0.0
        self.status = {"online": None}
        self._offline = None                               # reason, when the source says so
        self._said = set()                                 # one log line per distinct refusal

    # ── source callbacks (source thread) ─────────────────────────────────

    def _on_frame(self, t, cid, data, flags):
        if flags and flags.get("err"):
            self.errors += 1
            return
        cid = cid.upper()
        data = bytes(data)
        now = time.monotonic()
        with self.lock:
            self.latest[cid] = (now, t, data)
            self.recent[cid].append((now, data))
            cap = self._captures.get(cid)
            if cap is not None:
                cap.append(can_line(cid, data))
        self.frames += 1
        self.t_last = now

    def _on_status(self, status):
        status = dict(status or {})
        self.status = status
        if status.get("online") is False:
            self._offline = status.get("error") or "frame source offline"
        elif status.get("online"):
            self._offline = None

    # ── transport interface ──────────────────────────────────────────────

    async def connect(self, log=print):
        self._log = log
        await self.source.start(self._on_frame, self._on_status)
        self.adapter_name = self.source.name or self.adapter_name
        self.adapter_port = getattr(self.source, "port", "") or self.adapter_port
        mode = "LISTEN-ONLY" if self.listen_only else "normal"
        log(f"  NATIVE CAN — {self.adapter_name} ({self.adapter_port or 'no port'}), "
            f"bus {self.bus}, {mode} mode")
        if self.listen_only:
            log("  listen-only: no request will be sent on this bus; every UDS item answers NO DATA")
        if self.stmin_override is not None:
            log(f"  ISO-TP STmin forced to 0x{int(self.stmin_override):02X} by can_isotp_stmin "
                f"(ATFCSD is parsed but not used)")
        if self.settle:
            await asyncio.sleep(self.settle)       # let the table see a round of broadcasts
        if self.frames:
            log(f"  {self.frames} frames seen in the first {self.settle:.1f} s, "
                f"{len(self.latest)} distinct ids")

    async def close(self):
        await self.source.stop()

    async def send(self, cmd, wait=0.0, timeout=8.0):
        cmd = (cmd or "").strip()
        up = cmd.upper()
        if not cmd:                       # the "any char interrupts ATMA" poke
            return []
        self.commands += 1
        await asyncio.sleep(0)            # stay a real coroutine (yields to the loop)

        if up.startswith("AT"):
            return self._at(up[2:].strip(), timeout)

        if self._offline:
            raise ConnectionError(f"CAN source offline: {self._offline}")

        # A request: hex bytes, service first. Anything that does not parse is
        # a "?" — which is_no_data() already treats as no data.
        try:
            data = bytes.fromhex(up.replace(" ", ""))
        except ValueError:
            data = b""
        if not data:
            self._say(f"not a request: {cmd!r}")
            return ["?"]
        if self.listen_only:
            return self._refuse(up, f"refused {up}: the {self.bus} bus is listen-only, nothing is sent on it")
        if data[0] not in READ_SERVICES:
            return self._refuse(up, f"refused {up}: service 0x{data[0]:02X} is not a read service this "
                                    f"project sends (SECURITY.md allows 0x21, 0x01, 0x03, 0x07)")
        if not self.tx or not self.rx:
            self._say(f"{up} before ATSH/ATCRA — no target, answering NO DATA")
            self.misses.append(f"{self.tx or '?'}:{up}")
            return ["NO DATA"]
        return await self._request(up, data, timeout)

    # ── the AT surface (memo §4.2) ───────────────────────────────────────

    def _at(self, body, timeout):
        if body in ("Z", "I", "@1"):
            if body == "Z":
                self.tx = self.rx = self.fc_tx = None
                self.caf = True
                self.fc_bs = self.fc_stmin = 0
            if self._offline:
                return []                 # no digit → probe_alive says the link is gone
            return [self.adapter_name]
        if body.startswith("SH"):
            self.tx = body[2:].strip().replace(" ", "") or None
            return []
        if body.startswith("CRA"):
            self.rx = body[3:].strip().replace(" ", "") or None
            return []
        if body == "AR":                  # ATAR — automatic receive, filter off
            self.rx = None
            return []
        if body.startswith("CAF"):
            self.caf = body.endswith("1")
            return []
        if body.startswith("FCSH"):
            self.fc_tx = body[4:].strip().replace(" ", "") or None
            return []
        if body.startswith("FCSD"):
            self._parse_fcsd(body[4:])
            return []
        if body.startswith("FCSM"):
            try:
                self.fc_mode = int(body[4:].strip() or "1")
            except ValueError:
                pass
            return []
        if body == "MA":
            return self.monitor(timeout)
        return []                         # ATE0/ATL1/ATH1/ATS1/ATSP6/… → "OK"

    def _parse_fcsd(self, arg):
        """ATFCSD 30 00 20 → block size 0x00, STmin 0x20: the flow-control
        frame we send back to an ECU mid-answer. The bytes are what the ELM
        would put on the wire; the source pads them like the ELM does."""
        try:
            fc = bytes.fromhex(arg.strip().replace(" ", ""))
        except ValueError:
            return
        if len(fc) >= 3 and (fc[0] & 0xF0) == 0x30:
            self.fc_bs, self.fc_stmin = fc[1], fc[2]

    @property
    def stmin(self):
        """The STmin actually asked for: the override, else what ATFCSD set."""
        return int(self.stmin_override) if self.stmin_override is not None else self.fc_stmin

    # ── ATMA from the table ──────────────────────────────────────────────

    def monitor(self, window):
        """Frames of the filtered id (all ids if no ATCRA) that arrived within
        the last `window` seconds, oldest first, then forgotten — the same
        semantics as an ATMA dwell of `window`, delivered at once."""
        try:
            window = max(0.0, float(window or 0.0))
        except (TypeError, ValueError):
            window = 0.0
        cut = time.monotonic() - window
        out = []
        with self.lock:
            ids = [self.rx.upper()] if self.rx else list(self.recent)
            for cid in ids:
                dq = self.recent.get(cid)
                if not dq:
                    continue
                out.extend((t, cid, d) for t, d in dq if t >= cut)
                dq.clear()
        out.sort(key=lambda x: x[0])
        return [can_line(cid, d) for _, cid, d in out]

    def freshness(self):
        """{id: seconds since its last frame} — a true staleness source for
        native CAN (memo §4.8). Not yet wired into the reader."""
        now = time.monotonic()
        with self.lock:
            return {cid: round(now - t, 3) for cid, (t, _, _) in self.latest.items()}

    # ── UDS through the source ───────────────────────────────────────────

    async def _request(self, up, data, timeout):
        rx = self.rx.upper()
        with self.lock:
            self._captures[rx] = lines = []
        try:
            ack = await self.source.send_uds(self.tx, rx, data, self.fc_bs, self.stmin, timeout)
        except Exception as e:            # a dead bus mid-request: let the supervisor reconnect
            with self.lock:
                self._captures.pop(rx, None)
            raise ConnectionError(f"CAN request failed: {type(e).__name__}: {e}") from e
        ack = dict(ack or {})
        self.last_ack = ack
        # A source whose ack can overtake the frames (a broker delivers
        # topics independently) gets a short grace to let them land.
        expected = ack.get("frames") if ack.get("ok") else None
        if expected:
            deadline = time.monotonic() + 0.25
            while len(lines) < expected and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
        with self.lock:
            self._captures.pop(rx, None)
            got = list(lines)
        if not ack.get("ok") or not got:
            self.misses.append(f"{self.tx}:{up}")
            if not ack.get("ok") and ack.get("error") not in (None, "timeout"):
                self._say(f"{up} to {self.tx}: {ack.get('error')}")
            return ["NO DATA"]
        return got

    # ── bookkeeping ──────────────────────────────────────────────────────

    def _refuse(self, up, why):
        self.refused.append(f"{self.tx or '?'}:{up}")
        self._say(why)
        return ["NO DATA"]

    def _say(self, msg):
        if msg not in self._said:
            self._said.add(msg)
            self._log(f"  [can] {msg}")

    def liveness(self):
        d = dict(self.source.liveness() or {})
        d.setdefault("frames", self.frames)
        d["errors"] = d.get("errors", 0) + self.errors
        return d

    def marker(self):
        """Stamped into every record: which bus, and whether it could transmit."""
        return {"can_bus": self.bus, "listen_only": self.listen_only}


# ── the local source: python-can ─────────────────────────────────────────

class _Sink(can.Listener):
    """The one python-can listener: forwards every frame to the source's
    on_frame and keeps the counters liveness() reports. Runs on the
    Notifier's thread."""

    def __init__(self, source):
        self.source = source
        self.last_seen = {}                # arbitration id -> monotonic time
        self.counts = collections.Counter()
        self.frames = 0
        self.errors = 0
        self.t_last = 0.0
        self._sec = 0
        self._sec_count = 0
        self.fps = 0

    def on_message_received(self, m):
        now = time.monotonic()
        if m.is_error_frame:
            self.errors += 1
            self.source._deliver(m.timestamp, id_hex(m.arbitration_id, m.is_extended_id), b"", {"err": True})
            return
        if m.is_remote_frame:
            return
        self.frames += 1
        self.t_last = now
        sec = int(now)
        if sec != self._sec:
            self.fps = self._sec_count if sec == self._sec + 1 else 0
            self._sec, self._sec_count = sec, 0
        self._sec_count += 1
        self.last_seen[m.arbitration_id] = now
        self.counts[m.arbitration_id] += 1
        self.source._deliver(m.timestamp, id_hex(m.arbitration_id, m.is_extended_id),
                             bytes(m.data), {"ext": True} if m.is_extended_id else {})

    def on_error(self, exc):
        self.source._die(f"{type(exc).__name__}: {exc}")


class LocalSource(FrameSource):
    """A python-can bus: the CANable on USB (slcan or gs_usb firmware), a
    socketcan device on Linux, or the in-process `virtual` bus the tests use.

    `bus_factory`, when given, replaces the interface/channel logic entirely
    (memo §5): the tests pass a lambda that opens a virtual bus."""

    def __init__(self, interface="auto", channel="", bitrate=DEFAULT_BITRATE, bus="car",
                 listen_only=False, bus_factory=None, name=None, response_timeout=1.0,
                 tx_padding=0x00, libusb_path=None, fc_timeout_ms=1000, cf_timeout_ms=1000,
                 log=print):
        if bus not in BUSES:
            raise ValueError(f"can_bus must be one of {BUSES}, not {bus!r}")
        self.bus = bus
        # The EV bus is the battery, inverter and charger network: it is opened
        # listen-only whatever the config says, and there is no override.
        self.listen_only = bool(listen_only) or bus == "ev"
        self.interface = (interface or "auto").lower()
        self.channel = channel or ""
        self.bitrate = int(bitrate or DEFAULT_BITRATE)
        self.bus_factory = bus_factory
        self.name = name or ""
        self.port = ""
        self.response_timeout = float(response_timeout)
        self.tx_padding = tx_padding
        self.libusb_path = libusb_path
        self.fc_timeout_ms = int(fc_timeout_ms)
        self.cf_timeout_ms = int(cf_timeout_ms)
        self.log = log
        self.firmware = None
        self._bus = None
        self._notifier = None
        self._sink = None
        self._stacks = {}                  # (tx, rx, bs, stmin) -> isotp stack
        self._stack_lock = threading.Lock()
        self._on_frame = None
        self._on_status = None
        self._req_error = None
        self.isotp_errors = 0
        self.status = {"online": False}

    # ── contract ─────────────────────────────────────────────────────────

    async def start(self, on_frame, on_status):
        self._on_frame = on_frame
        self._on_status = on_status
        loop = asyncio.get_event_loop()
        self._bus = await loop.run_in_executor(None, self._open)
        self._sink = _Sink(self)
        self._notifier = can.Notifier(self._bus, [self._sink], timeout=0.1)
        self._set_status({"online": True})

    async def stop(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._close)
        self._set_status({"online": False})

    async def subscribe(self, ids):
        return None                        # a local controller receives everything

    async def send_uds(self, tx, rx, data, bs, stmin, timeout):
        data = bytes(data)
        if self.listen_only:
            return {"ok": False, "error": "refused"}
        if not data or data[0] not in READ_SERVICES:
            return {"ok": False, "error": "refused"}
        if self._bus is None or self.status.get("online") is False:
            return {"ok": False, "error": "bus-off"}
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._uds_sync, tx, rx, data, int(bs), int(stmin),
                                          float(timeout))

    def liveness(self):
        s = self._sink
        return {"online": bool(self.status.get("online")),
                "fps": s.fps if s else 0,
                "t_last": s.t_last if s else 0.0,
                "frames": s.frames if s else 0,
                "errors": (s.errors if s else 0) + self.isotp_errors,
                "adapter": self.name, "bus": self.bus, "listen_only": self.listen_only,
                "error": self.status.get("error")}

    # ── internals ────────────────────────────────────────────────────────

    def _deliver(self, t, cid, data, flags):
        if self._on_frame:
            self._on_frame(t, cid, data, flags)

    def _set_status(self, status):
        self.status = dict(status)
        if self._on_status:
            self._on_status(dict(status))

    def _die(self, reason):
        """The Notifier thread hit an error (USB gone, port closed, bus-off).
        Report offline; the façade's next send() raises and the reader reconnects."""
        if self.status.get("online") is not False:
            self.log(f"  [can] frame source failed: {reason}")
        self._set_status({"online": False, "error": reason})

    def _open(self):
        if self.bus_factory is not None:
            b = self.bus_factory()
            self.firmware = self.firmware or "factory"
            self.name = self.name or f"python-can {can.__version__} {getattr(b, 'channel_info', 'bus')}"
            self.port = self.port or str(getattr(b, "channel_info", "") or "")
            return b
        iface = self.interface
        if iface == "virtual":
            b = can.Bus(interface="virtual", channel=self.channel or "hakake", receive_own_messages=False)
            self.firmware = "virtual"
            self.name = self.name or f"python-can {can.__version__} virtual"
            self.port = f"virtual:{self.channel or 'hakake'}"
            return b
        if iface == "socketcan":
            return self._open_socketcan()
        if iface == "auto":
            kind, dev = sniff_canable()
            if kind == "dfu":
                raise ConnectionError(DFU_ADVICE)
            if kind == "gs_usb":
                return self._open_gs_usb(dev)
            if kind == "slcan":
                return self._open_slcan(dev)
            port = find_slcan_port(self.channel)
            if port:
                return self._open_slcan(None, port)
            raise ConnectionError("no CANable found: no USB device with a known CANable id "
                                  "(16d0:117e slcan, 1d50:606f candleLight) and no serial port "
                                  f"matching {' / '.join(SLCAN_PORT_PATTERNS)}; set can_interface / can_channel")
        if iface == "slcan":
            return self._open_slcan(None, self.channel or find_slcan_port(self.channel))
        if iface == "gs_usb":
            return self._open_gs_usb(None)
        raise ConnectionError(f"unknown can_interface {iface!r} (auto, slcan, gs_usb, socketcan, virtual)")

    def _open_slcan(self, dev, port=None):
        port = port or find_slcan_port(self.channel, dev)
        if not port:
            raise ConnectionError("CANable (slcan firmware) found on USB but no serial port for it — "
                                  f"expected {' or '.join(SLCAN_PORT_PATTERNS)}; set can_channel")
        vp = f" {dev.idVendor:04x}:{dev.idProduct:04x}" if dev is not None else ""
        try:
            b = HakakeSlcanBus(channel=port, bitrate=self.bitrate, listen_only=self.listen_only,
                               sleep_after_open=0.5)
        except Exception as e:
            raise ConnectionError(f"cannot open slcan on {port}: {type(e).__name__}: {e}") from e
        self.firmware = "slcan"
        self.name = self.name or f"CANable2 slcan{vp}"
        self.port = port
        if self.listen_only:
            self.log("  [can] slcan: asked for silent mode with M1 before O (stock canable2-fw); the "
                     "firmware does not acknowledge it, so it cannot be verified from here — this "
                     "process never calls send() on a listen-only bus regardless")
        return b

    def _open_gs_usb(self, dev):
        if sys.platform == "darwin":
            # pyusb must find Homebrew's libusb; ctypes reads this at lookup time (memo §2.1).
            os.environ.setdefault("DYLD_FALLBACK_LIBRARY_PATH", self.libusb_path or "/opt/homebrew/lib")
        try:
            from can.interfaces.gs_usb import GsUsbBus
            from gs_usb.constants import GS_CAN_MODE_LISTEN_ONLY, GS_CAN_MODE_HW_TIMESTAMP
        except ImportError as e:
            raise ConnectionError("candleLight firmware needs the gs_usb driver: "
                                  f"pip install \"python-can[gs-usb]\" ({e})") from e
        index = 0
        if self.channel.strip().isdigit():
            index = int(self.channel.strip())
        try:
            b = GsUsbBus(channel="canable", index=index, bitrate=self.bitrate)
        except Exception as e:
            raise ConnectionError(f"cannot open gs_usb device {index}: {type(e).__name__}: {e}") from e
        if self.listen_only:
            try:
                apply_listen_only_gs_usb(b.gs_usb, GS_CAN_MODE_LISTEN_ONLY, GS_CAN_MODE_HW_TIMESTAMP)
            except Exception:
                try:
                    b.shutdown()
                finally:
                    raise
            self.log("  [can] gs_usb: listen-only mode set and read back from the device")
        vp = f" {dev.idVendor:04x}:{dev.idProduct:04x}" if dev is not None else ""
        self.firmware = "gs_usb"
        self.name = self.name or f"candleLight gs_usb{vp}"
        self.port = f"gs_usb:{index}"
        return b

    def _open_socketcan(self):
        ch = self.channel or "can0"
        if self.listen_only and not socketcan_is_listen_only(ch):
            raise ConnectionError(f"{ch} is not in listen-only mode; the {self.bus} bus is only opened "
                                  f"silent: sudo ip link set {ch} up type can bitrate {self.bitrate} listen-only on")
        try:
            b = can.Bus(interface="socketcan", channel=ch)
        except Exception as e:
            raise ConnectionError(f"cannot open socketcan {ch}: {type(e).__name__}: {e}") from e
        self.firmware = "socketcan"
        self.name = self.name or f"socketcan {ch} (python-can {can.__version__})"
        self.port = ch
        return b

    def _close(self):
        if self._notifier is not None:
            try:
                self._notifier.stop()
            except Exception:
                pass
            self._notifier = None
        with self._stack_lock:
            for st in self._stacks.values():
                try:
                    st.stop()
                except Exception:
                    pass
            self._stacks.clear()
        if self._bus is not None:
            try:
                self._bus.shutdown()          # slcan sends C, so the next process opens a closed channel
            except Exception:
                pass
            self._bus = None

    # ── UDS via can-isotp (memo §4.3) ────────────────────────────────────

    def _isotp_error(self, exc):
        self.isotp_errors += 1
        self._req_error = f"{type(exc).__name__}: {exc}"

    def _stack(self, tx, rx, bs, stmin):
        import isotp
        key = (tx.upper(), rx.upper(), bs, stmin)
        with self._stack_lock:
            st = self._stacks.get(key)
            if st is not None:
                return st
            # One stack per (tx, rx); a changed BS/STmin gets a fresh one.
            for k in [k for k in self._stacks if k[:2] == key[:2]]:
                try:
                    self._stacks.pop(k).stop()
                except Exception:
                    pass
            addr = isotp.Address(isotp.AddressingMode.Normal_11bits, txid=int(tx, 16), rxid=int(rx, 16))
            params = {
                "stmin": stmin,                 # what WE ask the ECU for, from ATFCSD
                "blocksize": bs,
                "tx_padding": self.tx_padding,  # the ELM pads to 8 bytes with 00 (ATV0); so do we
                "rx_flowcontrol_timeout": self.fc_timeout_ms,
                "rx_consecutive_frame_timeout": self.cf_timeout_ms,
                "wftmax": 0,
                "max_frame_size": 4095,
                "can_fd": False,
            }
            st = isotp.NotifierBasedCanStack(self._bus, self._notifier, address=addr, params=params,
                                             error_handler=self._isotp_error)
            st.start()
            self._stacks[key] = st
            return st

    def _uds_sync(self, tx, rx, data, bs, stmin, timeout):
        rx_id = int(rx, 16)
        try:
            st = self._stack(tx, rx, bs, stmin)
            while st.recv(block=False) is not None:      # a stale answer is not this answer
                pass
            self._req_error = None
            before = self._sink.counts[rx_id]
            t0 = time.monotonic()
            st.send(data)
            deadline = t0 + max(0.05, timeout)
            first = t0 + self.response_timeout
            while True:
                payload = st.recv(block=True, timeout=0.02)
                if payload is not None:
                    return {"ok": True, "frames": self._sink.counts[rx_id] - before}
                now = time.monotonic()
                if self._req_error:
                    return {"ok": False, "error": f"isotp: {self._req_error}"}
                if self.status.get("online") is False:
                    return {"ok": False, "error": "bus-off"}
                if now >= deadline:
                    return {"ok": False, "error": "timeout"}
                # A silent ECU is the common case (car asleep): give up after
                # response_timeout unless an answer has started arriving.
                if now >= first and self._sink.last_seen.get(rx_id, 0.0) < t0:
                    return {"ok": False, "error": "timeout"}
        except Exception as e:
            return {"ok": False, "error": f"bus-off: {type(e).__name__}: {e}"}


# ── firmware-specific buses ──────────────────────────────────────────────


class HakakeSlcanBus(_SlcanBus):
    """python-can's slcan, with listen-only done the way the stock CANable 2
    firmware understands it. python-can sends `L`, which canable2-fw does not
    implement and does not acknowledge — the channel never opens. The stock
    firmware's silent switch is `M1` before `O` (memo §2.4); Elmue's 2.5
    firmware accepts the same legacy pair."""

    def open(self):
        if self._listen_only:
            self._write("M1")
        self._write("O")


def apply_listen_only_gs_usb(gs, listen_only_bit, timestamp_bit=0):
    """Re-open a gs_usb device in listen-only mode and VERIFY it took.

    python-can 4.6.1 starts the device in normal mode with no way to ask for
    another; `gs_usb.start()` masks the flags by what the firmware advertises
    and drops an unsupported bit silently. So: stop, start with the bit, read
    `device_flags` back, and refuse the bus if the bit is not there. A bus we
    cannot prove silent is not opened at all."""
    gs.stop()
    gs.start(listen_only_bit | timestamp_bit)
    flags = getattr(gs, "device_flags", None) or 0
    if not (flags & listen_only_bit):
        try:
            gs.stop()
        except Exception:
            pass
        raise ConnectionError("adapter firmware has no listen-only mode (device_flags "
                              f"0x{flags:x}) — not opening the bus silent, so not opening it")
    return flags


# ── finding the board (memo §2.3) ────────────────────────────────────────

def classify_usb(devices):
    """(kind, device) for the first CANable-class device: 'gs_usb', 'slcan',
    'dfu' or (None, None). `devices` are objects with idVendor / idProduct."""
    found = None
    for dev in devices:
        vp = (int(dev.idVendor), int(dev.idProduct))
        if vp in CANDLE_IDS:
            return "gs_usb", dev
        if vp in SLCAN_IDS:
            return "slcan", dev
        if vp in DFU_IDS and found is None:
            found = ("dfu", dev)          # keep looking: a real adapter beside a stuck one wins
    return found or (None, None)


def _usb_devices():
    """Every USB device pyusb can see; [] when pyusb / libusb is not there."""
    try:
        import usb.core
    except ImportError:
        return []
    try:
        return list(usb.core.find(find_all=True) or [])
    except Exception:                     # NoBackendError and friends: fall back to the port glob
        return []


def sniff_canable(devices=None):
    return classify_usb(_usb_devices() if devices is None else devices)


def find_slcan_port(channel="", dev=None):
    """The CDC serial port of an slcan CANable: the configured channel, else
    a port whose USB vid matches, else the first usbmodem / ttyACM port."""
    if channel and not channel.strip().isdigit():
        return channel
    try:
        from serial.tools import list_ports
        ports = list(list_ports.comports())
    except Exception:
        ports = []
    vids = {v for v, _ in SLCAN_IDS}
    for p in ports:
        if getattr(p, "vid", None) in vids:
            return p.device
    for pattern in SLCAN_PORT_PATTERNS:
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


def socketcan_is_listen_only(channel):
    """True when `ip -details link show <channel>` reports LISTEN-ONLY."""
    import subprocess
    try:
        out = subprocess.run(["ip", "-details", "link", "show", channel], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return False
    return "LISTEN-ONLY" in out.upper()


# ── configuration and the detect_adapter entry point (memo §4.5) ─────────

CONFIG_KEYS = ("can_interface", "can_channel", "can_bus", "can_listen_only", "can_bitrate",
               "can_isotp_stmin", "can_response_timeout", "can_tx_padding", "can_libusb_path")


def _truthy(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def can_config(cfg=None):
    """The `can_*` keys of config.local.json, HAKAKE_CAN_* environment winning.

    `can_bus` is required and has no default: the board cannot tell which
    pins it is wired to, and normal mode on EV-CAN would be a transmit-capable
    node on the battery bus. Raises ConnectionError so the reader's supervisor
    reports it instead of crashing."""
    cfg = dict(cfg or {})

    def get(key, default=None):
        v = env("HAKAKE_" + key.upper())
        return v if v is not None else cfg.get(key, default)

    bus = str(get("can_bus") or "").strip().lower()
    if bus not in BUSES:
        raise ConnectionError('config.local.json needs "can_bus": "car" or "ev" (which OBD pins the '
                              'adapter is wired to — 6/14 or 13/12); it is never guessed. '
                              'HAKAKE_CAN_BUS overrides it.')
    out = {
        "interface": str(get("can_interface", "auto") or "auto").lower(),
        "channel": str(get("can_channel", "") or ""),
        "bitrate": int(get("can_bitrate", DEFAULT_BITRATE) or DEFAULT_BITRATE),
        "bus": bus,
        "listen_only": _truthy(get("can_listen_only", False)) or bus == "ev",
        "response_timeout": float(get("can_response_timeout", 1.0) or 1.0),
        "libusb_path": get("can_libusb_path") or None,
    }
    stmin = get("can_isotp_stmin")
    out["stmin_override"] = None if stmin in (None, "") else int(str(stmin), 0)
    pad = get("can_tx_padding", 0)
    out["tx_padding"] = None if pad in (None, "", "none", "null") else int(str(pad), 0)
    return out


async def open_can(cfg=None, log=print, source_kwargs=None):
    """Build and connect the native CAN transport from configuration.
    Called by elm327.detect_adapter(prefer="can"); never by auto-detect."""
    opts = can_config(cfg)
    stmin_override = opts.pop("stmin_override")
    opts.update(source_kwargs or {})
    src = LocalSource(log=log, **opts)
    elm = CanFacade(src, stmin_override=stmin_override, log=log)
    await elm.connect(log=log)
    return elm
