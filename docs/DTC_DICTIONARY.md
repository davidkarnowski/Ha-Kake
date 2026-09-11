<!--
SPDX-FileCopyrightText: 2026 David D. Karnowski
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Trouble-code dictionary — the format, and how to build your own

> Ha-Kake ships the **format** for trouble-code descriptions and **none of the
> data**. This document is the recipe for building a dictionary for the car you
> actually own, with an AI agent, in an afternoon. The file you build stays on
> your machine.

## Why no dictionary ships

Short version: nobody who publishes DTC description text can show they are
allowed to.

- **SAE J2012**, the standard that defines the generic code descriptions, is a
  paid, copyrighted document. There is no open grant, and SAE's own terms say
  reproduction beyond the licence is cause for revocation.
- **Manufacturer text is service-manual text.** The Leaf descriptions in the
  tools people use carry Nissan Electronic Service Manual page references
  (`EVC-279`, `WT-26`) beside each row — about as clear a statement of
  derivation as one could ask for. The ESM is a paid subscription.
- **Every "open" dataset is the same table in a different wrapper**, and not
  one of them states where its text came from. An MIT or GPL header on a file
  does not cure the licence of content the uploader did not own.

So the licensing question is sidestepped rather than argued: the application
defines the schema and knows how to read a dictionary; you build the dictionary
for your own vehicle; it lives beside `config.local.json`, gitignored, and is
never committed. A dictionary you built from a service manual you pay for is
not yours to redistribute either — which is exactly why the project ships the
format and not the data.

**Nothing degrades without one.** With no dictionary the dashboard shows the
bare codes, as it always has. The description is an enrichment, and the code —
the thing actually read off the car — is what is stored either way.

## Where the file goes

| | |
|---|---|
| `dtc/<profile>.json` | the usual place, e.g. `dtc/lancer_2009.json`. `dtc/` is gitignored. |
| `dtc/generic.json` | codes any car can throw. Loaded *under* the profile file, so a manufacturer entry wins. |
| a profile's `DTC_FILES` | an optional tuple of extra file names in `vehicles/<profile>.py`, layered in its own order. |
| `config.local.json` → `"dtc"` | a path or a list of paths. When present it replaces the convention outright. |

Later files win per code. Check what the loader found:

```bash
python dtc.py                      # what would load for the default profile
python dtc.py dtc/lancer_2009.json # validate one file; non-zero exit on any problem
```

A missing file is normal and silent. A malformed one logs a single warning and
behaves as if it were absent — the page keeps working and keeps showing codes.

## The schema

**`dtc.py`'s module docstring is the authority**; this table quotes it. Read
`python -c "import dtc; help(dtc)"` if the two ever disagree.

A file is one object with a small header and an array of entries:

```json
{
  "schema": 1,
  "vehicle": "lancer_2009",
  "generated": "2026-09-11",
  "generator": "docs/DTC_DICTIONARY.md recipe v1",
  "note": "Built locally. Not redistributable unless every entry's source permits it.",
  "codes": [
    {
      "code": "P0420",
      "desc": "The catalytic converter is not cleaning the exhaust as well as the downstream sensor expects.",
      "scope": "generic",
      "evidence": "standard",
      "source": "SAE J2012 generic P04xx range"
    },
    {
      "code": "P1234",
      "desc": "Guessed from the neighbouring codes; nobody has confirmed this one.",
      "scope": "mitsubishi",
      "evidence": "unverified",
      "source": "inferred from the P12xx range — not confirmed"
    }
  ]
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `code` | string | **yes** | `^[PBCU][0-9A-F]{4}$`, uppercase. The primary key and the join to what the reader read. An **empty** `"code"` is a distinct trap from a missing one: it makes a junk lookup that silently matches nothing, so the validator rejects it. |
| `desc` | string | **yes** | One line, plain English, **in your own words**. Rejected if empty, if it is the code repeated back, or if it is a placeholder (`"Unknown"`, `"No Title"`, `"N/A"`, …). |
| `scope` | string | **yes** | Which authority defines the code: `generic`, `nissan`, `mitsubishi`, … |
| `evidence` | string | **yes** | One of the five tiers below. |
| `source` | string | **yes** | Where the meaning came from — a URL, a manual section, a named dataset. Never blank: "I don't know" is spelled `evidence: "unverified"`, not an empty source. |
| `module` | string | optional | The ECU that *defines* the code (`"LBC"`, `"TCU"`). Omit when unknown; never write `"unknown"`. |
| `desc_long` | string | optional | A paragraph, when there is genuinely more to say. |
| `causes` | string[] | optional | Short phrases. Omit the key rather than emitting `[]`. |
| `aliases` | string[] | optional | Other printable forms of the same fault. |

### Four fields that are deliberately missing

- **`ecu`** belongs to the *sighting*, not to the code. The same code can be
  reported by different modules, and the responding address is in hand at read
  time anyway.
- **`severity`** — every schema surveyed has this field and not one fills it.
  Guessing severity on a safety-adjacent EV fault is worse than showing
  nothing, and an agent asked for it will invent it three thousand times.
- **`system`** is derivable from the first letter (P/C/B/U → powertrain /
  chassis / body / network; `dtc.system_of()` does it). Storing it only invites
  it to disagree with the code.
- **`possible_fixes`** — this is a read-only telemetry dashboard, not a repair
  manual, and that field is precisely where copyrighted manual text ends up.

The cautionary example is a public dictionary whose entries carry fourteen
fields of which three are filled: `"severity": "unknown"`, `"standard":
"Unknown"`, `"title": "No Title"`, `"sources": []`. **A schema with fourteen
fields and three real ones is worse than one with five that are all real**,
because it teaches the reader to ignore the fields. That is why the validator
rejects placeholders instead of accepting them politely.

### The `evidence` tiers

| Value | Meaning |
|---|---|
| `service-manual` | Read from the manufacturer's own manual. |
| `standard` | A generic SAE J2012 code whose meaning is fixed by the standard. |
| `observed` | Seen on *this* vehicle and the meaning confirmed against the symptom. The strongest tier in practice, because it is about your car. |
| `community` | Forum consensus, a tool's strings, an unattributed dataset. Plausible, unconfirmed. |
| `unverified` | Inferred from the code's range and its neighbours. A guess, honestly labelled. |

**`community` and `unverified` descriptions are marked wherever they are
shown**, and that is enforced in `dtc.py`, not by the page's good manners: the
tier is part of the string the server produces, so `P1234 — … (unverified)`
reaches every renderer already marked. An unverified guess displayed like a
service-manual quote turns a known unknown into a confident wrong answer, which
is worse than no description at all.

## Building one with an agent

The tone matters more than the wording. Ask an agent to **research**, not to
**generate**: an agent told to produce 200 descriptions will produce 200, and
they will read exactly like the real ones. The nine points below are the whole
recipe; hand them to your agent along with your vehicle's make, model and year.

**1. Scope it from what the car can actually throw, not from the code space.**
"All P-codes" is ~2500 generic entries plus a manufacturer range, nearly all of
which your car cannot emit. Two better scopes, in order:

  - *Enumerate the car.* Some modules will list every code they know about when
    asked with a wide status mask. That list **is** the scope: vehicle-specific,
    free, and read-only. Capture it first and hand it to the agent. (On a
    first-generation Leaf that is a `19 02 FF` read to the battery controller —
    the research notes record a capture of 149 entries. Ha-Kake does not read
    codes from the Leaf yet; that is a separate, car-side piece of work.)
  - *Otherwise seed and grow.* Start with the codes the car has actually
    reported, plus the generic ranges relevant to the vehicle type — for an EV,
    `P0Axx`, `P0Bxx`, `P3xxx` and the `U0xxx` network codes. Add an entry when a
    new code appears.

**2. Name the authority per make, and say what it costs.**

  - Nissan Leaf — the Electronic Service Manual, <https://www.nissan-techinfo.com/>
    (paid subscription). LeafSpy Pro's descriptions are ESM-derived; a user who
    owns LeafSpy is reading the authority through a tool.
  - Mitsubishi Lancer — the Mitsubishi service manual for the model year, for
    the `P1xxx` range; SAE J2012 for everything generic.
  - Generic codes — SAE J2012 itself,
    <https://saemobilus.sae.org/standards/j2012_201612-diagnostic-trouble-code-definitions>.
  - Community corroboration — for the Leaf, <https://mynissanleaf.com/>, which
    is where first-generation knowledge actually lives.

**3. Record provenance per entry, always.** Every entry carries `source` and
`evidence`. A URL, a manual section, or a dataset name — never blank. Say it to
the agent explicitly: *if you cannot name where a meaning came from, the entry's
tier is `unverified` and you must say so in the entry rather than omit the
problem.*

**4. Forbid the plausible guess.** This is the failure mode that makes
AI-built dictionaries dangerous, because an interpolated description reads
exactly like a researched one. **An entry inferred from the code's range is
`evidence: "unverified"`, and no entry at all is better than a confident
invention.** A short, honest dictionary is a success. A suspiciously complete
one is a red flag — if your agent comes back with 3000 entries and no gaps, it
generated them.

**5. Own the words.** Re-word every description; do not transcribe manual or
dataset text verbatim. This is the licensing safeguard, and usefully it is also
a comprehension check: an agent that cannot restate a fault in its own words did
not understand it, and the entry should drop a tier.

**6. A file named after a make is often not a file about that make.** Two
real examples worth quoting to the agent:

  - `Wal33D/dtc-database` advertises 28,220 codes; its `nissan_codes.txt` is
    **59 internal-combustion `P1xxx` lines with zero EV codes**, and its
    `mitsubishi_codes.txt` is **34 lines**.
  - `Automotive-9/dtc-codes`'s `Nissan.json` has **3409 entries and not one
    `P0A` or `P3` code** — `P0AA6`, `P3030`, `P0A1F` and `P3102` are all absent.

An agent that greps for "nissan" and trusts the filename will fill a Leaf
dictionary with Sentra codes.

**7. Check the licence before quoting, and know that a tag is not a chain of
title.** `ircama/ELM327-emulator` is CC BY-**NC**-SA 4.0 (GitHub reports
`NOASSERTION`; the LICENSE text is NonCommercial), which is incompatible with
this project's AGPL-3.0 *and* with CC BY-SA 4.0. `Automotive-9/dtc-codes` has
**no licence file at all**, which under default copyright means all rights
reserved. `brendan-w/python-OBD` is GPL-2.0-only. And a permissive tag on
aggregated data proves nothing about where the data came from: `lucasveneno/dtc`
is MIT and its own `data/raw/batches/` directory contains a file named
`ext_scavenged_final.json`.

**8. Emit the schema above and validate it.** `python dtc.py <file>` checks
that the JSON parses, that `code` matches `^[PBCU][0-9A-F]{4}$`, that `desc` is
neither empty nor a placeholder nor the code repeated, that `evidence` is one of
the five tiers, that `source` is non-empty, that no key is an empty string, and
that there are no duplicate codes. It exits non-zero on any problem, so it drops
into a hook. `tests/fixtures/dtc_sample.json` is a three-entry synthetic file to
copy the shape from — every code and description in it is invented.

**9. Say where the file goes, and that it stays there.** `dtc/<profile>.json`,
machine-local, gitignored, alongside `config.local.json`. A dictionary built
from a service manual is not yours to redistribute. The privacy sweep
(`scripts/privacy_sweep.py`) fails if a dictionary is ever staged as a tracked
file — by path *and* by content, so renaming it out of `dtc/` does not get it
past.

### Confirmed-open sources your agent may consult

Pointing an agent at a dataset to **read** — to corroborate a meaning you then
write in your own words — is a different act from **redistributing** it. Nothing
below may be vendored into this repository or into the dictionary verbatim.
The licences are what each uploader asserted, verified from repository metadata;
they are not a warranty of chain of title, and point 7 applies to all of them.

| Repository / file | Licence | Coverage | Why it might help |
|---|---|---|---|
| `BirchJD/PiOBDII` → `DATA/TroubleCodes-ISO-SAE.txt` | GPL-3.0 (ships the GPL text; GitHub detects no SPDX id) | 3456 codes (2480 P, 252 B, 162 C, 562 U) — including the whole generic `P0Axx` hybrid/EV range | The best *generic* set found, and the only open one covering the EV range. Plain `CODE Description` lines. |
| `dalathegreat/Battery-Emulator` → `web_data/dtc/nissan_leaf_dtc.json` | GPL-3.0 (repo) | 204 Leaf battery-controller entries, dense in the `P3xxx` cell-controller range | The only Leaf-specific dictionary found anywhere. Its text reads as service-manual-derived and its provenance is undocumented — a cross-check, never a source. |
| `mytrile/obd-trouble-codes` → `obd-trouble-codes.csv` | MIT | 3071 codes; no `P0Axx` | Oldest of the family and likely the ancestor of several others. |
| `mickeyl/LTSupportAutomotive` → `Base.lproj/Localizable.strings` | MIT | 3394 codes; no `P0Axx`; German and French translations | MIT and actively used. |
| `OBDb` org (`OBDb/Nissan-Leaf`, `OBDb/Mitsubishi-Lancer`) → `signalsets/v3/default.json` | **CC BY-SA 4.0** | Signals and PIDs only — **no DTC dictionary exists in the org** | Not a code source, but its licence matches this project's docs and its Leaf test cases corroborate the group reads. |

Avoid, per point 7: `ircama/ELM327-emulator` (NonCommercial), `Automotive-9/dtc-codes`
(no licence), `brendan-w/python-OBD` (GPL-2.0-only).

## What the dashboard does with it

The Lancer's `dtc_stored`, `dtc_pending` and `dtc_trans` signals are plain text
— a space-separated list of printable codes, or the literal `none`. Descriptions
are added on the way *out* of `/api/status`, never on the way in, so what the
store holds is the raw code that was read off the car:

```
"dtc_stored": "P0171 — The engine is running lean on bank 1. · P2195 (unverified)"
"dtc_raw":    {"dtc_stored": "P0171 P2195"}
"dtc_desc":   {"dtc_stored": [{"code": "P0171", "desc": "…", "evidence": "service-manual",
                               "marked": false, "system": "powertrain", "display": "…"}, …]}
```

A code with no entry shows as the bare code; a list with nothing to add comes
back untouched. `dtc_desc` is there for any richer view that wants the tier and
the source rather than a line of text.

To make a signal in another profile behave this way, mark its registry entry
`"dtc": True` — that declaration is the only thing outside `vehicles/` that says
which signals carry codes.

## Related

- `dtc.py` — the loader, the validator, and the authority on the schema.
- `docs/SIGNALS.md` — the code signals themselves, per vehicle.
- `SECURITY.md` — reading codes is a read; **clearing** them is a write and does
  not exist anywhere in this project.
