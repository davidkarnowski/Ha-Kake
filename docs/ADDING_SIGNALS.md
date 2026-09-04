# Adding a new input — the routine

Every signal on the dashboard went through the same six steps. Follow them in
order; each one is small, and skipping one is how "tentative" decodes get
mistaken for facts.

## 0. Know where it lives

| The signal is… | Then it is… | Tool |
|---|---|---|
| broadcast on Car-CAN (gear, TPMS, doors) | a **passive** item: one CAN ID, captured with `ATCAF0` | `probe_hvac_carcan.py --phase A`, `gear_hvac_live.py gear` |
| answered by the LBC (battery) | an **lbc** item: UDS `0x79B → 0x7BB`, service `21 NN` | `battery_read.py --raw` |
| answered by another ECU (HVAC amp …) | a **uds** item with its own tx/rx pair | `probe_hvac_carcan.py --phase B` |
| only on EV-CAN (`0x1DB`, `0x54F` …) | **not reachable** without a re-pinned cable — stop here | — |

## 1. Capture it, with the physical input changing

For anything with discrete settings, use the walker — it pauses and resumes
the dashboard itself, prompts you step by step, captures each step, and
ranks the bytes that follow the input (and checks the way back down):

```bash
python calibrate_input.py fan                       # fan 1→7→1 on the HVAC amp
python calibrate_input.py custom --steps "off,1,2,3,2,1,off" --target lbc
```

For continuous or passive signals, pause the dashboard (`touch
web/reader.pause`), run `probe_hvac_carcan.py` / `gear_hvac_live.py` while
you change the thing, and save the raw lines. A capture where nothing
changed proves nothing. Remove the pause file when done.

Put the raw JSON in `tests/fixtures/` with the date in the filename. Personal
or noisy dumps go in `research/` instead (gitignored).

## 2. Decode it in `leaf_decoders.py`

- Passive frame → add a block to `decode_carcan()` (use `last_complete_frame`
  so a truncated BLE line cannot produce garbage).
- LBC group → `decode_groupNN()`; HVAC → `decode_hvac()`.
- Temperatures: emit both `*_c` and `*_f` (`c_to_f`). Current: negative =
  discharge.

## 3. Pin it with a test

`tests/test_carcan_hvac.py` / `tests/test_decoders.py`: feed the fixture in,
assert the decoded value you saw with your own eyes. One assertion per
transition you observed is ideal (see `test_gear_421_live_values`).

## 4. Make the reader fetch it — the vehicle profile (`vehicles/leaf_ze0.py`)

- `ITEMS`, `TILES`, `ITEM_KEYS` and the `SIGNALS` registry all live in the
  vehicle profile (`vehicles/leaf_ze0.py` for the Leaf).
- New source? Add an entry to `ITEMS` with a `period` (0 = every cycle; be
  stingy — every fast item costs ~0.4 s per cycle over BLE).
- If a built-in tile shows it, add the item to that tile's `items` in `TILES`.
- New keys that should vanish when the item is disabled → `ITEM_KEYS`.

## 5. Register it — the profile's `SIGNALS`

One entry: `key` (dotted index allowed), `label`, `kind` (`number` — the
default — or `bool`/`text`, which the UI renders as lamp/text tiles), `unit`,
`min`/`max`, `dec`, `item`, default `color` scale, optional `hist` (the name
this value has in the stored history — see step 5b), optional `alt` +
`alt_unit` (the °C twin of an °F value).

That single entry makes the signal available in the **Tiles ▾ → add** menu
with every renderer (number, ring, arc, dial, bar, thermometer, battery,
line/area/bars, text, lamp) and every colour scale, and tells the reader
which item a user tile needs.

## 5b. Make it graphable — the profile's `HISTORY_COLS`

Values not listed in `HISTORY_COLS` ride in the store's `extra` JSON bag: kept
forever, but not charted and not aggregated. To graph or aggregate one, add a
column to the **profile's** `HISTORY_COLS` — never to `web/store.py`, which
builds the schema, the insert, `history()` and `daily_health()` from whatever
the active profile declares:

```python
"coolant_temp_c": {"kind": "real", "hist": "coolant_temp", "round": 1,
                   "hist_f": "coolant_temp_f",              # the °F twin
                   "daily": {"avg": "coolant_temp_c"}},     # daily_health()
```

`kind` is `real`/`int`/`bool`/`text`; `key` defaults to the column name and may
be a dotted list index (`"temps.0"`) or a callable taking the record. The
`hist` name is what a signal's `hist` field points at. The migration is
additive and self-healing: the column appears in existing databases on the next
start and is backfilled from `extra`. The full spec is the contract docstring
in `vehicles/__init__.py`; `python vehicles/__init__.py` checks your profile.

## 6. Document it — `docs/SIGNALS.md`

Add the row with **verified** or **tentative**, the sample you saw, and the
date. Append what you did to `WORKLOG.md`. Commit the decoder, test, item,
registry entry and docs together: `signals: <what> from <ID/group>`.

## Checklist

- [ ] fixture captured while the input changed
- [ ] decoder emits °C + °F / signed current where relevant
- [ ] test asserts the observed value(s)
- [ ] `ITEMS` / `TILES` / `ITEM_KEYS` updated
- [ ] profile `SIGNALS` entry (+ `HISTORY_COLS` column if graphable)
- [ ] `python vehicles/__init__.py` reports the profile OK
- [ ] `docs/SIGNALS.md` row + `WORKLOG.md` entry
- [ ] `pytest -q` (venv active) green, privacy sweep OK
