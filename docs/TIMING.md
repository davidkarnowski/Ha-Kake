# Timing — what every timestamp means, and which clock it came from

> **Status (2026-09-09):** the design below is implemented and covered by
> `tests/test_timing.py` (acquisition stamps, the two clocks, `ts_source`,
> the envelope, the page helper, the self-test on replay). **Nothing here has
> been measured on a native CAN adapter, a Pi bridge or the car yet**: every
> number a doc quotes for those must come from `tools/bench_transport.py
> --timing` when they arrive, and until then the offsets are expectations, not
> results. The ELM path (BLE / USB) has run on the car for weeks under the
> same rules; the new keys just say what was always true.

This page is the authority on the timestamps in a record and a row. Where it
disagrees with a comment in the code, fix one of them in the same commit.

## 1. Three clocks, two uses

| Clock | Where | Used for | Never used for |
|---|---|---|---|
| **Monotonic** (`loop.time()`) | the reader | scheduling: `item_last`, `item_age`, the store period, cycle timing | anything stored |
| **Wall** (`time.time()`, UTC) | the reader | storage: a row's `ts` / `ts_epoch`, `item_ts`, events, sessions | scheduling |
| **Source** (`t` on a frame) | python-can (the driver), or the bridge (a Pi's clock, over MQTT) | `frame_ts`, the per-session `clock_offset_s` | correcting a stored value |

The rule: **monotonic for scheduling, wall-clock for storage, never mixed.**
A laptop that sleeps, or has its clock stepped by NTP, must not make an item
look overdue or fresh; and a row must carry the time a human can line up with
a flag, a photo or another log. `tests/test_timing.py` audits the reader for
both.

## 2. Keys in the record and where they go

Every record the reader publishes (`web/battery_state.json`, `/api/status`,
the MQTT `state` topic) carries, per item polled:

| Key | Type | Meaning | Stored |
|---|---|---|---|
| `timing[item]` | s | how long that item's request took this cycle | `extra` |
| `item_age[item]` | s | seconds since the item last ran, at emission (monotonic) | `extra` |
| `item_ts[item]` | ISO ms `Z` | **when the item's value was acquired** (wall clock) | rebuilt from the epoch |
| `item_ts_epoch[item]` | epoch s | the same, as a number | `extra` |
| `frame_ts[item]` | epoch s | the *source's* timestamp of the newest frame behind a passive item; only on a transport with a source clock | `extra` |
| `ts_source` | text | whose clock stamps the rows: `laptop` (ELM, replay, sim), `driver` (python-can), `bridge` (a Pi over MQTT) | its own column |
| `clock_offset_s` | s | `median(t_rx − t_src)` over the last ≤ 200 frames; absent on an ELM | `sessions.clock_offset_s` |
| `cycle_s` | s | the whole cycle | `extra` |
| `timestamp` | ISO s `Z` | the cycle's wall time; a row's `ts` is this | column |

**What "acquired" means.** A UDS answer (`lbc01`, `hvac10`, a mode-01 PID) is
stamped the moment it returns. A passive item is stamped by its *newest
frame's arrival*, which on a transport that keeps a frame table (the native
CAN façade, MQTT through the same façade) can be older than the cycle that
reported it — no new `0x421` came in, so the value and its stamp are both from
before. On an ELM the frames arrived during the `ATMA` dwell that has just
ended, so the return time is within `secs` of the truth and that is what is
recorded. The sticky cache makes this matter: a record mixes a current read
0.1 s ago with a cell set read 20 s ago, and `item_ts` is the only thing in
the *database* that says so — `item_age` is relative to emission and is
meaningless once the row is old.

The page shows it as the per-tile "read at" badge (`Playback.itemAge()` in
`web/static/playback.js`): live, measured from *now*, so the badge keeps
counting between polls; in playback, measured from the frame's own moment —
the age as it *was*, because a recorded frame is not stale.

## 3. The two clocks are both kept

A frame from python-can carries `msg.timestamp` (the driver's clock, epoch
seconds, hardware-stamped on some adapters); a frame from the bridge carries
the Pi's `t`. The façade stamps every frame again on receipt with this
machine's wall clock, keeps the pair per id (`CanFacade.source_times()`) and
a deque of the differences, and the reader publishes the median as
`clock_offset_s` and writes it to the session whenever it changes.

**Nothing is corrected.** A stored row's time is the laptop's; a stored
`frame_ts` is the source's; the offset between them is *evidence* stored
beside them, so that a later reader — including one that learns the bridge's
clock was wrong all afternoon — can reconstruct either. Expectations, to be
replaced by measurements: the bridge runs chrony against the same LAN NTP as
the laptop, so tens of milliseconds on the LAN; unknown over a VPN, which is
exactly what the stored offset is for. A python-can driver clock is the same
kernel's, so the offset should be USB latency (single-digit ms) plus the
Notifier thread's wake-up.

## 4. Peak-preserving decimation

The reader publishes every cycle (~0.5–2 s) but stores a row every
`STORE_PERIOD` (5 s), overwriting in between; the cell log is the one
exception. A native transport turns that into 100 Hz current under a 5 s
sample, and a pull's peak lands between rows. So a profile marks the few keys
where a peak matters:

```python
HISTORY_COLS = {
    "current_a": {"kind": "real", "hist": "current_a", "round": 3, "peak": True},
    ...
}
```

(`peak: True` is also accepted on a `SIGNALS` entry; the key must be a plain
numeric record key, and the validator says so otherwise.) The Leaf marks
`pack_v`, `current_a`, `power_kw` and `cell_min`. For each, the reader keeps a
running envelope over the values **`decode()` produced** since the last
stored row — never the sticky cache, so a value read once is counted once —
and writes it into that row's `extra`:

| Key | Meaning |
|---|---|
| `<key>_min`, `<key>_max` | the extremes since the previous row |
| `<key>_tmin`, `<key>_tmax` | when each happened (epoch; the producing item's `item_ts`) |
| `<key>_n` | how many samples the envelope saw — 1 on a BLE cycle, hundreds on a native one |

The envelope resets on every stored row (a cell-log row included). The
stored *sample* (`current_a` the column) is still the value the car reported
last; the envelope is beside it, not instead of it. `Store.pulls()` — the
auto-flags on the timeline — judges a row by `current_a_min` when it is
there, so the true peak of a pull counts even when the 5 s sample missed it,
and `t_peak` is the peak's own time. The strip drawing the envelope is the
next step (`docs/PLAYBACK.md` "Resolution").

## 5. The self-test

```
python tools/bench_transport.py --timing --adapter replay --seconds 20
python tools/bench_transport.py --timing --adapter usb --seconds 600
python tools/bench_transport.py --timing --adapter mqtt --seconds 600 --json
```

Runs the reader's own `poll_once` over the adapter against a throwaway
in-memory store (no state file, no MQTT publish, nothing touches the real
database) and reports: the cycle count and the cycle-period distribution
with its jitter (stdev); per item, the `timing` distribution (min / median /
p95 / max) and the `item_age` at emission; and, on a transport with a source
clock, `clock_offset_s` first / last / min / max and the drift per minute.
`tests/test_timing.py` runs it on replay for a fraction of a second. The
numbers this project's docs quote for a transport's timing come from this
tool, not from a model (the older sweep in the same file measures the raw
USB link; the two answer different questions).

## 6. What is not here yet

- **The high-rate `samples` table** (plan §8.5): per-frame timestamps for the
  ring-buffer ids, flushed on a flag. The envelope above is the cheap half of
  the same need.
- **Fixtures and captures** convert absolute `t` to offsets from the first
  frame (the privacy rule); `ts_source` on a replayed row is `laptop`, because
  the replay adapter answers on this machine's clock.
- Measured offsets and jitter for `can` and `mqtt` — the board and the Pi
  have not arrived.
