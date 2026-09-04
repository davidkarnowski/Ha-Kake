#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
ELM327 Transport Abstraction — BLE and USB Serial

Provides a common async interface for communicating with ELM327-based OBD-II
adapters over either BLE (bleak) or USB serial (pyserial).

Classes:
  BleELM     — BLE adapter (LELink "OBDBLE" or similar)
  SerialELM  — USB serial adapter (CH340-based or similar)
  ReplayELM  — a recorded session fixture, no hardware (--adapter replay)
  SimELM     — a running vehicle model, no hardware (--adapter sim)

Functions:
  detect_adapter(prefer=None)  — auto-detect connected adapter
  configure_uds(elm, tx, rx)   — reset + ISO-TP setup for one ECU conversation
  set_uds_target(elm, tx, rx)  — re-point an already configured adapter
  passive_capture(elm, can_id) — raw frame capture (ATCAF0 + ATCRA + ATMA)

Vehicle profiles own their own setup: vehicles/<name>.configure(elm). The one
vehicle-specific helper still living here, configure_leaf_bms(), is a thin
wrapper over configure_uds() kept because vehicles/leaf_ze0.py imports it.

Usage:
  elm = await detect_adapter()        # auto-detect (hardware only, never sim/replay)
  elm = await detect_adapter("usb")   # force USB
  elm = await detect_adapter("sim")   # force the simulator (asked for, never guessed)
  await configure_uds(elm, "79B", "7BB")
  lines = await elm.send("2101")
  await elm.close()
"""

import asyncio
import glob
import json
import os
import threading
import time

from util import env

_ROOT = os.path.dirname(os.path.abspath(__file__))
LOCAL_CONFIG = os.path.join(_ROOT, "config.local.json")   # gitignored; see config.local.example.json


def load_local_config():
    """Per-machine settings (adapter address etc.). Env vars win over the file."""
    cfg = {}
    try:
        with open(LOCAL_CONFIG) as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    # HAKAKE_* is the current spelling; the older LEAF_* names still work.
    addr = env("HAKAKE_BLE_ADDR", "LEAF_BLE_ADDR")
    if addr:
        cfg["ble_addr"] = addr
    name = env("HAKAKE_BLE_NAME", "LEAF_BLE_NAME")
    if name:
        cfg["ble_name"] = name
    return cfg


# ── BLE Adapter ─────────────────────────────────────────────────────────

_cfg = load_local_config()
BLE_ADDR = _cfg.get("ble_addr") or ""          # empty → find by name at connect time
BLE_NAME = _cfg.get("ble_name") or "OBDBLE"
BLE_FFE1 = "0000ffe1-0000-1000-8000-00805f9b34fb"


class BleELM:
    """ELM327 over BLE (bleak)."""

    adapter_type = "ble"

    # The reference transport: the `est` numbers in the vehicle profiles were
    # timed over this link, so its cost multiplier is 1.0 by definition.
    SPEED = 1.0

    def __init__(self, address=BLE_ADDR, characteristic=BLE_FFE1):
        self.address = address
        self.characteristic = characteristic
        self.client = None
        self.adapter_name = ""
        self.adapter_port = address
        self._buf = ""
        self._raw_response = ""
        self._cmd_event = asyncio.Event()
        self._disconnected = False

    def _on_disconnect(self, _client):
        # CoreBluetooth tears the link down on lid-close / sleep; flag it and
        # release any waiting send so the reader's supervisor can reconnect.
        self._disconnected = True
        try:
            self._cmd_event.set()
        except Exception:
            pass

    def _on_notify(self, _sender, data: bytearray):
        text = bytes(data).decode("ascii", errors="replace")
        self._buf += text
        self._raw_response += text
        if ">" in self._buf:
            self._cmd_event.set()
            self._buf = ""

    async def connect(self, log=print):
        # macOS/CoreBluetooth invalidates a peripheral's session UUID across sleep,
        # so a direct connect-by-address fails after a lid-close with "not found".
        # Always re-discover the device first (by address if we have one, else by
        # advertised name), then connect to the found BLEDevice.
        from bleak import BleakClient, BleakScanner
        dev = None
        if self.address:
            log(f"  scanning for adapter {self.address} …")
            dev = await BleakScanner.find_device_by_address(self.address, timeout=8.0)
        if dev is None:
            log(f"  scanning for adapter by name {BLE_NAME!r} …")
            dev = await BleakScanner.find_device_by_name(BLE_NAME, timeout=6.0)
        if dev is None:
            raise ConnectionError(
                f"BLE adapter not found (addr={self.address or '?'}, name={BLE_NAME!r}); "
                f"is Bluetooth on and the dongle powered?")
        self.address = dev.address
        self.adapter_port = dev.address
        log(f"  found {dev.name or BLE_NAME} at {dev.address}, connecting …")
        self.client = BleakClient(dev, disconnected_callback=self._on_disconnect)
        await asyncio.wait_for(self.client.connect(), timeout=20.0)
        self._disconnected = False
        await self.client.start_notify(self.characteristic, self._on_notify)
        await asyncio.sleep(0.2)

        # Identify adapter
        lines = await self.send("ATI", wait=0.3, timeout=3.0)
        self.adapter_name = lines[0] if lines else "ELM327 (BLE)"
        log(f"  connected: {self.adapter_name}")

    async def close(self):
        if self.client and self.client.is_connected:
            try:
                await self.client.stop_notify(self.characteristic)
            except Exception:
                pass
            await self.client.disconnect()

    async def send(self, cmd, wait=0.3, timeout=8.0):
        if self._disconnected or not (self.client and self.client.is_connected):
            raise ConnectionError("BLE link is down")
        self._buf = ""
        self._raw_response = ""
        self._cmd_event.clear()
        try:
            await asyncio.wait_for(
                self.client.write_gatt_char(
                    self.characteristic, (cmd + "\r").encode(), response=True),
                timeout=max(3.0, timeout),
            )
        except (asyncio.TimeoutError, Exception) as e:
            self._disconnected = True
            raise ConnectionError(f"BLE write failed: {type(e).__name__}: {e}")
        try:
            await asyncio.wait_for(self._cmd_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        if self._disconnected:
            raise ConnectionError("BLE link dropped mid-command")
        await asyncio.sleep(wait)
        resp = self._raw_response.replace(">", "").strip()
        if resp.startswith(cmd):
            resp = resp[len(cmd):].strip()
        return [
            l.strip()
            for l in resp.replace("\r", "\n").split("\n")
            if l.strip() and l.strip() != "OK"
        ]


# ── USB Serial Adapter ──────────────────────────────────────────────────

SERIAL_PATTERNS = ["/dev/tty.usbserial-*", "/dev/cu.usbserial-*"]
SERIAL_BAUD = 38400          # every ELM327 powers up here

# ATBRD divisors: baud = 4_000_000 / divisor. Only rates the chip documents.
BRD_DIVISOR = {38400: 0x68, 57600: 0x45, 115200: 0x23, 230400: 0x11, 500000: 0x08}


def serial_target_baud():
    """The rate to negotiate up to after connecting (HAKAKE_SERIAL_BAUD).

    **Default 38400 — the rate every ELM327 powers up at.** Set
    HAKAKE_SERIAL_BAUD=115200 to opt in to the faster wire.

    Negotiating is worth roughly 2x on a long answer (a 29-frame 2102 is ~200 ms
    of wire at 38400 and ~65 ms at 115200), but it is the one thing here that
    leaves *persistent state on the adapter*: ATBRD survives the process, so a
    crashed or killed run leaves the chip fast while the next one opens slow and
    hears silence. That regression cost a debugging session on 2026-09-03. The
    large win — a command round-trip of ~107 ms down to ~15 ms — came from the
    blocking read and dropping the post-prompt sleep, neither of which touches
    the device. So the safe majority is the default and the risky remainder is
    opt-in. 230400 and 500000 are in the divisor table but this clone answers
    garbage or nothing at them.
    """
    raw = (env("HAKAKE_SERIAL_BAUD") or "").strip().lower()
    if raw in ("0", "off", "no", "none", "false"):
        return SERIAL_BAUD
    if not raw:
        return SERIAL_BAUD
    try:
        return int(raw)
    except ValueError:
        return SERIAL_BAUD


class SerialELM:
    """ELM327 over USB serial (pyserial)."""

    adapter_type = "usb"

    # Cost of one poll relative to BLE, for Reader.estimate(). The `est`
    # numbers in the vehicle profiles were timed over BLE; the same commands
    # over USB, measured 2026-09-03 with tools/bench_transport.py at 115200:
    # a short UDS read 9 ms against a 450 ms BLE est (0.02x), and a 435-byte
    # answer 44 ms against a 1.3 s est (0.03x). 0.1 is deliberately several
    # times more pessimistic than either: the car was asleep, so a woken ECU
    # returning full frames could not be timed, and over-packing a cycle is
    # worse than under-packing it.
    SPEED = 0.1

    # SimSerialELM turns this off: a pseudo-terminal has no wire rate to
    # negotiate, and the simulator does not implement ATBRD.
    negotiate_baud = True

    def __init__(self, port=None, baud=SERIAL_BAUD):
        self.port = port
        self.baud = baud
        self.ser = None
        self.adapter_name = ""
        self.adapter_port = port or ""

    async def connect(self):
        import serial as pyserial

        if not self.port:
            self.port = _find_serial_port()
        if not self.port:
            raise ConnectionError("No USB serial port found")

        self.adapter_port = self.port
        self.ser = pyserial.Serial(
            port=self.port,
            baudrate=self.baud,
            bytesize=pyserial.EIGHTBITS,
            parity=pyserial.PARITY_NONE,
            stopbits=pyserial.STOPBITS_ONE,
            timeout=1,
            write_timeout=1,
        )
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

        # Identify adapter
        lines = await self.send("ATI", wait=0.3, timeout=3.0)
        if not lines and self.negotiate_baud:
            # Silence at the power-up rate usually means a previous run left
            # the adapter at a negotiated rate and died before resetting it.
            # Go looking rather than declaring the adapter dead.
            lines = await self._recover_baud()
        self.adapter_name = lines[0] if lines else "ELM327 (USB)"

        if self.negotiate_baud:
            await self.set_baud(serial_target_baud())

    async def _recover_baud(self):
        """Find an adapter that a crashed run left at a non-default rate."""
        for baud in sorted(BRD_DIVISOR, reverse=True):
            if baud == self.baud:
                continue
            self.ser.baudrate = baud
            time.sleep(0.25)                     # CH340 needs longer than 0.1 to settle
            # The failed attempt at the wrong rate left noise in the chip's
            # input buffer, so the next command lands appended to garbage and
            # comes back "?". Send a bare CR to end that partial line first.
            try:
                self.ser.write(b"\r")
                time.sleep(0.15)
            except Exception:
                pass
            self.ser.reset_input_buffer()
            lines = await self.send("ATI", wait=0.1, timeout=1.5)
            # "?" is not a failure — it is the chip saying it heard us and did
            # not understand, which only happens at the *right* wire rate.
            # Clear and ask once more rather than moving on.
            if lines and lines[0].strip() == "?":
                self.ser.write(b"\r")
                time.sleep(0.15)
                self.ser.reset_input_buffer()
                lines = await self.send("ATI", wait=0.1, timeout=1.5)
            if lines and any(c.isdigit() for c in " ".join(lines)):
                self.baud = baud
                return lines
        self.ser.baudrate = SERIAL_BAUD
        self.baud = SERIAL_BAUD
        return []

    async def set_baud(self, target):
        """Negotiate a faster wire rate with ATBRD. Returns True if it took.

        The handshake is fussy and the fussiness is the whole story: the chip
        answers OK at the *old* rate, then sends its ID at the *new* one and
        waits ~75 ms for a bare CR back before it will keep the new rate. Read
        the prompt after the OK — the obvious thing to do — and the window is
        gone and the adapter silently reverts. That single misread is why this
        clone looked like it "did not support ATBRD".

        Every failure path puts us back where we started, so a clone that
        really cannot do it just keeps running at 38400.
        """
        div = BRD_DIVISOR.get(int(target))
        if div is None or int(target) == self.baud:
            return int(target) == self.baud

        def _negotiate():
            old = self.baud
            self.ser.reset_input_buffer()
            self.ser.write(f"ATBRD {div:02X}\r".encode("ascii"))
            self.ser.timeout = 1.0
            # Echo may still be on (ATZ turns it back on), so skip the echoed
            # command line before believing what we read.
            ack = b""
            for _ in range(3):
                ack = self.ser.read_until(b"\r")
                if not ack.strip() or b"ATBRD" not in ack.upper():
                    break
            if b"OK" not in ack.upper():
                return False                      # '?' — divisor not supported
            self.ser.baudrate = target
            self.ser.timeout = 0.3
            ident = self.ser.read_until(b"\r")
            if not ident.strip():
                self.ser.baudrate = old
                time.sleep(0.2)
                self.ser.reset_input_buffer()
                return False
            self.ser.write(b"\r")
            self.ser.timeout = 0.5
            ok = self.ser.read_until(b">")
            if b"OK" not in ok.upper():
                self.ser.baudrate = old
                time.sleep(0.2)
                self.ser.reset_input_buffer()
                return False
            self.ser.reset_input_buffer()
            return True

        loop = asyncio.get_event_loop()
        moved = await loop.run_in_executor(None, _negotiate)
        if moved:
            self.baud = int(target)
        return moved

    async def close(self):
        # Leave the adapter at its power-up rate: ATBRD is not persistent, but
        # only a reset clears it, and an unclean exit would otherwise leave the
        # next process talking 38400 to a 115200 chip.
        if self.ser and self.ser.is_open and self.baud != SERIAL_BAUD:
            try:
                self.ser.write(b"ATZ\r")
                time.sleep(0.3)
            except Exception:
                pass
        if self.ser and self.ser.is_open:
            self.ser.close()

    async def send(self, cmd, wait=0.3, timeout=8.0):
        loop = asyncio.get_event_loop()
        reset = cmd.replace(" ", "").upper() == "ATZ"
        if reset and self.baud != SERIAL_BAUD:
            # The answer to this ATZ arrives at 38400 while we are listening at
            # the negotiated rate, so it is unreadable by definition. Don't sit
            # out the full timeout waiting for a prompt that cannot be parsed —
            # the resync below is what actually re-establishes the link.
            timeout = min(timeout, 0.5)
        lines = await loop.run_in_executor(None, self._send_sync, cmd, wait, timeout)
        # ATZ is a real reset: the chip drops straight back to 38400 and
        # answers there. A negotiated link would go deaf at exactly the moment
        # a vehicle profile calls configure_uds(), which starts with ATZ — so
        # follow it down and negotiate back up. Nothing outside this class
        # has to know the wire rate ever changed.
        if reset and self.baud != SERIAL_BAUD:
            target, self.baud = self.baud, SERIAL_BAUD
            self.ser.baudrate = SERIAL_BAUD
            await asyncio.sleep(0.3)
            self.ser.reset_input_buffer()
            lines = await self._send_sync_async("ATI", 0.0, 2.0)
            await self.set_baud(target)
        return lines

    async def _send_sync_async(self, cmd, wait, timeout):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._send_sync, cmd, wait, timeout)

    def _send_sync(self, cmd, wait, timeout):
        # Blocking read to the prompt. The old loop polled in_waiting and slept
        # 50 ms when idle, so every command was rounded up to the next tick:
        # measured, an ATI the adapter answered in 11 ms took 55 ms to come
        # back. read_until() returns the moment the '>' lands and is still
        # bounded by `timeout`, which is also what ATMA relies on — monitor
        # mode never sends a prompt, so the read simply runs for `timeout`
        # seconds and returns whatever frames arrived, exactly as before.
        self.ser.reset_input_buffer()
        self.ser.write((cmd + "\r").encode("ascii"))

        self.ser.timeout = timeout
        response = self.ser.read_until(b">")

        # `wait` is deliberately ignored here. It exists for BLE, where a
        # reply arrives as a series of 20-byte notifications and a late chunk
        # can land after the one carrying '>'. On a byte stream the prompt is
        # the definitive end of the answer, so the sleep bought nothing and
        # cost real time: the reader asks for wait=0.05 on every poll, which
        # made a 5 ms ATI take 56 ms — an order of magnitude, for nothing.

        text = response.decode("ascii", errors="replace").replace(">", "").strip()
        if text.startswith(cmd):
            text = text[len(cmd):].strip()
        return [
            l.strip()
            for l in text.replace("\r", "\n").split("\n")
            if l.strip() and l.strip() != "OK"
        ]


# ── Detection & Configuration ───────────────────────────────────────────

def _find_serial_port():
    """Find first available USB serial port.

    HAKAKE_SERIAL_PORT overrides the glob. That is how the dashboard is
    pointed at the pseudo-terminal published by hakake_sim.py --pty: a pty is
    /dev/ttysNNN and matches no usbserial pattern, and pointing the *real*
    serial transport at it is the strongest test we have that the simulator
    is indistinguishable from a car.
    """
    forced = env("HAKAKE_SERIAL_PORT")
    if forced:
        return forced
    for pattern in SERIAL_PATTERNS:
        ports = sorted(glob.glob(pattern))
        if ports:
            return ports[0]
    return None


async def detect_adapter(prefer=None, log=print):
    """Auto-detect an ELM327 adapter. Returns a connected instance.

    Args:
        prefer: "usb", "ble", "replay", or None for auto (tries USB first).

    "replay" never touches hardware: it serves a recorded session fixture
    (HAKAKE_REPLAY_FIXTURE, else the active profile's default). Auto-detect
    never picks it — replay must be asked for, so recorded data can never be
    mistaken for a live car.

    "sim" is the same bargain one step further from the car: a running model
    (simulator/) answers instead of a recording, and can be changed mid-run
    through the control API. Auto-detect never picks it either. Configured by
    HAKAKE_SIM_{SCENARIO,SEED,SPEED,KNOBS,CONTROL_PORT}.

    Raises:
        ConnectionError if no adapter found.
    """
    errors = []

    if prefer == "sim":
        serial_path = env("HAKAKE_SIM_SERIAL")
        if serial_path:
            # The model is in another process, behind a pty published by
            # hakake_sim.py --pty. We reach it with the *real* serial
            # transport — the point of the exercise — but the mode is still
            # "sim", so the throwaway database and the simulated stamp apply.
            elm = SimSerialELM(port=serial_path)
            await elm.connect()
            log(f"  SIMULATOR over a real serial device: {serial_path}")
            log(f"  adapter answers: {elm.adapter_name}")
            log("  *** SIMULATED DATA — not a reading from any vehicle ***")
            return elm
        seed = env("HAKAKE_SIM_SEED")
        port = env("HAKAKE_SIM_CONTROL_PORT")
        try:
            knobs = json.loads(env("HAKAKE_SIM_KNOBS") or "{}")
        except json.JSONDecodeError as e:
            raise ConnectionError(f"HAKAKE_SIM_KNOBS is not valid JSON: {e}")
        elm = SimELM(scenario=env("HAKAKE_SIM_SCENARIO") or None,
                     seed=int(seed) if seed not in (None, "") else None,
                     knobs=knobs or None,
                     speed=float(env("HAKAKE_SIM_SPEED", default="1") or 1),
                     control_port=int(port) if port not in (None, "") else None)
        await elm.connect(log=log)
        return elm

    if prefer == "replay":
        elm = ReplayELM(path=env("HAKAKE_REPLAY_FIXTURE") or None,
                        loop=env("HAKAKE_REPLAY_LOOP", default="1") not in ("0", "no", "false"),
                        speed=float(env("HAKAKE_REPLAY_SPEED", default="1") or 1))
        await elm.connect(log=log)
        return elm

    if prefer in (None, "usb"):
        port = _find_serial_port()
        if port:
            try:
                elm = SerialELM(port=port)
                await elm.connect()
                print(f"  Adapter: {elm.adapter_name} (USB: {elm.adapter_port})")
                return elm
            except Exception as e:
                errors.append(f"USB ({port}): {e}")

    if prefer in (None, "ble"):
        try:
            elm = BleELM()
            await elm.connect(log=log)
            log(f"  Adapter ready: {elm.adapter_name} (BLE: {elm.adapter_port})")
            return elm
        except Exception as e:
            errors.append(f"BLE: {e}")

    detail = "; ".join(errors) if errors else "no adapters configured"
    raise ConnectionError(f"No ELM327 adapter found ({detail})")


async def configure_uds(elm, tx, rx):
    """Reset the adapter and set it up for a UDS conversation with one ECU.

    Sets up:
      - ELM327 basics (echo off, headers on, spaces on, CAN 500k 11-bit)
      - ISO-TP addressing for the request/response pair (tx → rx)
      - Flow control parameters
    """
    await elm.send("ATZ", wait=1.5)
    for cmd in ("ATE0", "ATL1", "ATH1", "ATS1", "ATSP6",
                f"ATSH {tx}", f"ATCRA {rx}", "ATCAF1", f"ATFCSH {tx}",
                "ATFCSD 30 00 20", "ATFCSM1"):
        await elm.send(cmd, wait=0)


async def configure_leaf_bms(elm):
    """Nissan Leaf LBC/BMS (0x79B → 0x7BB). vehicles/leaf_ze0.py imports this
    as its configure(); the name stays for that contract."""
    await configure_uds(elm, "79B", "7BB")


# ── Multi-target helpers (UDS to other ECUs, passive Car-CAN capture) ────

async def set_uds_target(elm, tx, rx, full=False):
    """Point the adapter at another ECU: request header tx, response filter rx.

    ATFCSD / ATFCSM persist across header changes, so only the four header /
    filter / flow-control-header commands are needed when switching (full=False).
    AT commands finish at the prompt, so no post-prompt wait is needed.
    """
    await elm.send("ATCAF1", wait=0)
    await elm.send(f"ATSH {tx}", wait=0)
    await elm.send(f"ATCRA {rx}", wait=0)
    await elm.send(f"ATFCSH {tx}", wait=0)
    if full:
        await elm.send("ATFCSD 30 00 20", wait=0)
        await elm.send("ATFCSM1", wait=0)


async def passive_capture(elm, can_id, seconds=0.5, set_caf=True):
    """Capture raw frames for one CAN ID with ATCAF0 (no ISO-TP formatting).

    Returns a list of 'ID B0 B1 ...' lines. Leaves the adapter in CAF0 with the
    filter still set; call set_uds_target() (or configure_uds()) afterwards.
    Pass set_caf=False after the first call in a block to skip the ATCAF0 round-trip.
    """
    if set_caf:
        await elm.send("ATCAF0", wait=0)
    await elm.send(f"ATCRA {can_id}", wait=0)
    lines = await elm.send("ATMA", wait=0.0, timeout=seconds)
    await elm.send("", wait=0.05, timeout=2.0)   # any char interrupts monitor mode
    cid = can_id.upper()
    return [l for l in lines if l.upper().startswith(cid + " ") and len(l.split()) >= 2]


# ── Replay adapter (no hardware) ─────────────────────────────────────────
#
# The whole stack — reader, scheduler, profile configure()/decode(), store,
# API, dashboard — runs against a recorded session instead of a car. This is
# what makes a vehicle profile writable (and reviewable) by someone who does
# not own the car.
#
# Session-fixture format (see docs/REPLAY.md):
#
#   {"hakake_replay": 1,
#    "vehicle": "leaf_ze0",              # profile the capture came from
#    "adapter": "ELM327 v1.5",           # what ATI answers
#    "synthetic": false,                 # true == not recorded from a car
#    "source": ["tests/fixtures/..."],   # provenance, human-readable
#    "frames": [
#      {"t": 0.0,
#       "uds":     {"79B": {"2101": ["7BB 10 29 61 01 ...", ...]}},
#       "passive": {"421": ["421 08 00 00"]}}
#    ]}
#
# `uds` is keyed by the *request* header (what ATSH selects) then by command;
# lines are the raw ELM327 lines exactly as an adapter returns them, and are
# filtered against the live ATCRA filter the way real hardware does.
# Frames are cumulative: the view at time t is frames 0..i merged, so a frame
# only has to carry what changed. Anything the fixture does not contain
# answers NO DATA — the same thing a car that did not reply would say. The
# replay adapter never invents a value.

REPLAY_MARKER = "hakake_replay"


class ReplayFixtureError(ValueError):
    """The file is not a Ha-Kake replay session fixture."""


def replay_fixture_path(vehicle=None, root=None):
    """Default session fixture for a profile: tests/fixtures/session_<name>.json."""
    if vehicle is None:
        from vehicles import active_vehicle           # lazy: vehicles imports elm327
        vehicle = active_vehicle().NAME
    return os.path.join(root or _ROOT, "tests", "fixtures", f"session_{vehicle}.json")


def load_replay_fixture(path):
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict) or REPLAY_MARKER not in data:
        raise ReplayFixtureError(
            f"{path} is not a replay session fixture (no {REPLAY_MARKER!r} key). "
            f"Build one with:  python record_session.py --derive")
    frames = data.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ReplayFixtureError(f"{path}: 'frames' must be a non-empty list")
    for n, fr in enumerate(frames):
        if not isinstance(fr, dict):
            raise ReplayFixtureError(f"{path}: frame {n} is not an object")
        for section in ("uds", "passive"):
            if not isinstance(fr.get(section, {}), dict):
                raise ReplayFixtureError(f"{path}: frame {n} '{section}' must be an object")
    return data


class ReplayELM:
    """ELM327 look-alike backed by a recorded session fixture. No hardware."""

    adapter_type = "replay"
    replay = True

    def __init__(self, path=None, loop=True, speed=1.0, vehicle=None):
        self.path = path or replay_fixture_path(vehicle)
        self.loop = loop
        self.speed = max(0.01, float(speed or 1.0))
        self.data = load_replay_fixture(self.path)
        self.fixture_name = os.path.basename(self.path)
        self.vehicle = self.data.get("vehicle")
        self.synthetic = bool(self.data.get("synthetic"))
        self.frames = self.data["frames"]
        self.adapter_name = f"{self.data.get('adapter', 'ELM327 v1.5')} (replay: {self.fixture_name})"
        self.adapter_port = self.path
        self.adapter_id = self.data.get("adapter", "ELM327 v1.5")
        # adapter state a real ELM327 keeps
        self.tx = None            # ATSH — request header (which ECU we address)
        self.rx = None            # ATCRA — response filter
        self.caf = True           # ATCAF1 (ISO-TP formatting) vs ATCAF0 (raw)
        self.commands = 0
        self.misses = []          # commands the fixture had no answer for
        self.max_frame = 0        # furthest frame the session actually reached
        self._t0 = None
        self._views = {}
        # sanity: a frame's t must not go backwards
        self._times = [float(f.get("t", i)) for i, f in enumerate(self.frames)]
        self.duration = (max(self._times) - min(self._times)) + 1.0

    # ── timeline ─────────────────────────────────────────────────────────

    def elapsed(self):
        if self._t0 is None:
            return 0.0
        return (time.monotonic() - self._t0) * self.speed

    def frame_index(self, now=None):
        e = self.elapsed() if now is None else now
        if self.loop and self.duration > 0:
            e = e % self.duration
        base = self._times[0]
        idx = 0
        for i, t in enumerate(self._times):
            if t - base <= e + 1e-9:      # float slop must not skip a frame
                idx = i
            else:
                break
        self.max_frame = max(self.max_frame, idx)
        return idx

    def view(self, idx):
        """Frames 0..idx merged — later frames override earlier ones per key."""
        if idx in self._views:
            return self._views[idx]
        uds, passive = {}, {}
        for fr in self.frames[:idx + 1]:
            for hdr, cmds in (fr.get("uds") or {}).items():
                uds.setdefault(hdr.upper(), {}).update(
                    {k.upper(): v for k, v in cmds.items()})
            for cid, lines in (fr.get("passive") or {}).items():
                passive[cid.upper()] = lines
        self._views[idx] = (uds, passive)
        return self._views[idx]

    # ── transport interface ──────────────────────────────────────────────

    async def connect(self, log=print):
        self._t0 = time.monotonic()
        log(f"  REPLAY — no adapter, no car. Fixture: {self.path}")
        log(f"  replaying {len(self.frames)} frame(s) of {self.vehicle or 'unknown profile'} data"
            + (f", {self.speed}x" if self.speed != 1.0 else "")
            + (", looping" if self.loop else ""))
        if self.synthetic:
            log("  *** SYNTHETIC fixture — these values were NOT recorded from a car ***")
        for line in self.data.get("source", []) or []:
            log(f"    source: {line}")

    async def close(self):
        self._t0 = None

    def _filter(self, lines):
        """Apply the live ATCRA filter the way the adapter does."""
        if not self.rx:
            return list(lines)
        pre = self.rx.upper() + " "
        kept = [l for l in lines if l.upper().startswith(pre)]
        # A line with no header at all (ATH0 capture) passes — we cannot tell.
        return kept or [l for l in lines if not l.split()[:1] or len(l.split()[0]) != 3]

    async def send(self, cmd, wait=0.0, timeout=8.0):
        cmd = (cmd or "").strip()
        up = cmd.upper()
        if not cmd:                       # the "any char interrupts ATMA" poke
            return []
        self.commands += 1
        await asyncio.sleep(0)            # stay a real coroutine (yields to the loop)

        if up.startswith("AT"):
            body = up[2:].strip()
            if body in ("Z", "I", "@1"):
                return [self.adapter_id]
            if body.startswith("SH"):
                self.tx = body[2:].strip().replace(" ", "") or None
                return []
            if body.startswith("CRA"):
                self.rx = body[3:].strip().replace(" ", "") or None
                return []
            if body == "AR":              # ATAR — automatic receive, filter off
                self.rx = None
                return []
            if body.startswith("CAF"):
                self.caf = body.endswith("1")
                return []
            if body == "MA":
                return self.monitor()
            return []                     # ATE0/ATL1/ATH1/ATS1/ATSP6/ATFC* → "OK"

        uds, _ = self.view(self.frame_index())
        table = uds.get((self.tx or "").upper())
        if table is None and len(uds) == 1:
            table = next(iter(uds.values()))          # single-ECU fixture, no ATSH yet
        lines = (table or {}).get(up)
        if lines is None:
            self.misses.append(f"{self.tx or '?'}:{up}")
            return ["NO DATA"]
        return self._filter(lines)

    def monitor(self):
        """ATMA — raw frames for whatever ATCRA is filtering on."""
        _, passive = self.view(self.frame_index())
        if self.rx:
            return list(passive.get(self.rx.upper(), []))
        out = []
        for lines in passive.values():
            out.extend(lines)
        return out


# ── Simulator adapter (no hardware, no recording) ────────────────────────
#
# Where ReplayELM serves a *recorded* session, SimELM serves a *generated*
# one: a running physical model of the vehicle (simulator/, see
# docs/SIMULATOR_CONTRACT.md) answers the same UDS and monitor commands a car
# would. The difference that earns its keep is that a simulator can be
# changed while it runs — drop the SOC, degrade a cell, put the car to sleep —
# and the dashboard reacts. A recording cannot do that.
#
# The rules replay established hold here too, and matter more:
#
#   * Auto-detect never picks it. Simulated data must be asked for.
#   * Every record it produces is stamped `simulated: true` with the scenario
#     and seed, so nothing downstream can mistake a model for a car.
#   * It gets its own database and state file; the real store is never opened.
#   * Anything the model has no answer for returns NO DATA. Nothing is
#     invented at this layer — the model is the only thing allowed to make a
#     number up, and it says so.
#
# Two ways to drive it:
#
#   web/app.py --adapter sim        SimELM in-process inside the reader; the
#                                   optional control server (--sim-control)
#                                   lets an agent turn knobs mid-run.
#   hakake_sim.py --pty             the model behind a real pseudo-terminal,
#                                   driven through the real SerialELM. Slower,
#                                   but it exercises the serial transport and
#                                   the dashboard genuinely cannot tell.

SIM_MARKER = "simulated"


def _cra_filter(rx, lines):
    """Apply an ATCRA filter the way the adapter does (shared by sim/replay)."""
    if not rx:
        return list(lines)
    pre = rx.upper() + " "
    kept = [l for l in lines if l.upper().startswith(pre)]
    # A line with no header at all (ATH0 capture) passes — we cannot tell.
    return kept or [l for l in lines if not l.split()[:1] or len(l.split()[0]) != 3]


def make_simulator(vehicle=None, scenario=None, seed=None, knobs=None):
    """Build a simulator core from the contract's factory.

    Isolated in one function so the import failure has one clear message and
    so tests can substitute a stub without touching the transport.
    """
    try:
        from simulator import make_sim
    except ImportError as e:                    # pragma: no cover - depends on core
        raise ConnectionError(
            "the simulator core (simulator/) is not available: "
            f"{e}. --adapter sim needs it; use --adapter replay for a recorded session.")
    if vehicle is None:
        from vehicles import active_vehicle     # lazy: vehicles imports elm327
        vehicle = active_vehicle().NAME
    kw = {"vehicle": vehicle, "knobs": knobs or {}, "scenario": scenario}
    if seed is not None:
        kw["seed"] = seed          # the factory owns the default seed, not us
    return make_sim(**kw)


class SimELM:
    """ELM327 look-alike backed by a running simulator. No hardware, no car.

    Holds exactly the adapter state a real ELM327 keeps between commands —
    ATSH (request header), ATCRA (response filter), ATCAF (ISO-TP formatting)
    — and hands everything else to the model. `handle()` is the synchronous
    core so the pty server in hakake_sim.py can share this one state machine
    rather than growing a second, subtly different one.
    """

    adapter_type = "sim"
    simulated = True

    def __init__(self, sim=None, vehicle=None, scenario=None, seed=None,
                 knobs=None, speed=1.0, control_port=None):
        self.sim = sim if sim is not None else make_simulator(vehicle, scenario, seed, knobs)
        self.scenario = scenario or getattr(self.sim, "scenario", None) or "default"
        self.seed = seed if seed is not None else getattr(self.sim, "seed", None)
        self.vehicle = vehicle or getattr(self.sim, "vehicle", None)
        self.speed = max(0.01, float(speed or 1.0))
        self.control_port = control_port
        self.control_url = None
        self._control = None
        self.lock = threading.RLock()           # HTTP control threads vs. the reader loop
        self.adapter_id = "ELM327 v1.5"
        self.adapter_name = f"ELM327 v1.5 (sim: {self.scenario})"
        self.adapter_port = f"sim:{self.scenario}"
        # adapter state a real ELM327 keeps
        self.tx = None            # ATSH  — request header (which ECU we address)
        self.rx = None            # ATCRA — response filter
        self.caf = True           # ATCAF1 (ISO-TP formatting) vs ATCAF0 (raw)
        self.commands = 0
        self.misses = []          # commands the model had no answer for
        self.sim_time = 0.0       # seconds of simulated time advanced so far
        self._last = None

    # ── clock ────────────────────────────────────────────────────────────

    def advance(self, now=None):
        """Step the model by the wall-clock time since the last command.

        The model is only ever advanced from here, so `--speed 10` is a single
        multiplier and a paused reader does not silently drift the car.
        """
        now = time.monotonic() if now is None else now
        if self._last is None:
            self._last = now
            return 0.0
        dt = max(0.0, (now - self._last)) * self.speed
        self._last = now
        if dt > 0:
            with self.lock:
                self.sim.step(dt)
            self.sim_time += dt
        return dt

    # ── the one ELM327 state machine ─────────────────────────────────────

    def handle(self, cmd, timeout=0.5):
        """One command in, ELM327 response lines out. Synchronous."""
        cmd = (cmd or "").strip()
        up = cmd.upper()
        if not cmd:                       # the "any char interrupts ATMA" poke
            return []
        self.commands += 1
        self.advance()

        if up.startswith("AT"):
            body = up[2:].strip()
            if body in ("Z", "I", "@1"):
                return [self.adapter_id]
            if body.startswith("SH"):
                self.tx = body[2:].strip().replace(" ", "") or None
                return []
            if body.startswith("CRA"):
                self.rx = body[3:].strip().replace(" ", "") or None
                return []
            if body == "AR":              # ATAR — automatic receive, filter off
                self.rx = None
                return []
            if body.startswith("CAF"):
                self.caf = body.endswith("1")
                return []
            if body == "MA":
                return self.monitor(timeout)
            return []                     # ATE0/ATL1/ATH1/ATS1/ATSP6/ATFC* → "OK"

        with self.lock:
            lines = self.sim.respond(up, self.tx, self.rx)
        lines = list(lines or [])
        if not lines or lines == ["NO DATA"]:
            self.misses.append(f"{self.tx or '?'}:{up}")
            return ["NO DATA"]
        return _cra_filter(self.rx, lines)

    def monitor(self, secs=0.5):
        """ATMA — raw frames for whatever ATCRA is filtering on."""
        with self.lock:
            lines = self.sim.frames(self.rx, max(0.0, float(secs or 0.0)))
        return _cra_filter(self.rx, list(lines or []))

    # ── transport interface ──────────────────────────────────────────────

    async def connect(self, log=print):
        self._last = time.monotonic()
        log("  SIMULATOR — no adapter, no car. Every value below is generated.")
        log(f"  model: {self.vehicle or 'unknown profile'}  scenario: {self.scenario}  seed: {self.seed}"
            + (f"  clock {self.speed}x" if self.speed != 1.0 else ""))
        if self.control_port is not None:       # 0 is a request too: "any free port"
            self.serve_control(self.control_port, log=log)
        log("  *** SIMULATED DATA — not a reading from any vehicle ***")

    async def close(self):
        self._last = None
        if self._control is not None:
            try:
                self._control.shutdown()
            except Exception:
                pass
            self._control = None

    async def send(self, cmd, wait=0.0, timeout=8.0):
        await asyncio.sleep(0)            # stay a real coroutine (yields to the loop)
        return self.handle(cmd, timeout=timeout)

    # ── agent control surface ────────────────────────────────────────────

    def serve_control(self, port, log=print):
        """Start the HTTP knob server on 127.0.0.1:<port> in a daemon thread.

        A busy port falls back to a free one instead of raising. This runs
        inside the reader child, and `web/app.py` restarts that child whenever
        it dies — so an OSError here (a stale `hakake_sim.py --pty` still
        holding 8099, or a second dashboard) was a silent crash loop: no
        readings, "restarting in 2s" forever. Now the model runs, the record
        carries the port actually bound, and the log says loudly which port
        was wanted and which was used. Port 0 is already "any free port", so
        there is nothing to fall back to and its error propagates.
        """
        from hakake_sim import serve_control      # lazy: utility, not a dependency
        wanted = int(port or 0)
        self.control_port_requested = wanted
        try:
            self._control = serve_control(self.sim, wanted, lock=self.lock, log=log)
        except OSError as e:
            if wanted == 0:
                raise
            self._control = serve_control(self.sim, 0, lock=self.lock, log=log)
            bound = self._control.server_address[1]
            log(f"  *** sim control port {wanted} is busy ({e.strerror or e}) — "
                f"serving on {bound} instead. Is a stale `hakake_sim.py --pty` or "
                f"another dashboard holding {wanted}? (`lsof -i :{wanted}`) ***")
        self.control_port = self._control.server_address[1]
        self.control_url = f"http://127.0.0.1:{self.control_port}"
        log(f"  sim control API: {self.control_url}/sim/schema  (GET/POST — see docs/SIMULATOR.md)")
        return self._control

    def marker(self):
        """The fields that label a record as generated, for the state file."""
        return {SIM_MARKER: True,
                "sim_scenario": self.scenario,
                "sim_seed": self.seed,
                "sim_vehicle": self.vehicle,
                "sim_control_url": self.control_url or ""}


class SimSerialELM(SerialELM):
    """The real serial transport, pointed at a simulator's pseudo-terminal.

    Everything about the link is genuine — pyserial, a tty, ELM327 text — so
    this exercises the code path a USB dongle uses. Only the thing on the far
    end is imaginary, and that is exactly what has to stay visible: the class
    exists so `simulated` and the marker ride along, and the reader keeps
    using the throwaway database instead of the real one.

    Selected with `--adapter sim --sim-serial /dev/ttysNNN`, never by
    auto-detect, and never by `--adapter usb` (which would write generated
    rows into a database of real readings).
    """

    simulated = True
    negotiate_baud = False        # a pty has no wire rate, and no ATBRD

    def marker(self):
        return {SIM_MARKER: True,
                "sim_scenario": "pty",
                "sim_seed": None,
                "sim_vehicle": None,
                "sim_transport": f"serial {self.adapter_port}",
                "sim_control_url": env("HAKAKE_SIM_CONTROL_URL") or ""}
