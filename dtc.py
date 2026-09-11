#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Trouble-code dictionary: the format, the loader, and the lookup.

**This project ships the format and no data.** SAE J2012, which defines the
generic DTC descriptions, is a paid copyrighted standard; the Leaf-specific
text in the tools people use carries Nissan service-manual page references, so
it derives from the paid ESM; and every "open" DTC dataset surveyed for this
feature turned out to be the same table in a different wrapper with no stated
chain of title. A permissive licence header on aggregated text does not cure
the licence of content the uploader did not own. So the application defines
the schema, reads a dictionary from a machine-local file, and
``docs/DTC_DICTIONARY.md`` carries a recipe a user runs with an AI agent to
build one for the vehicle they actually own. The result stays local, exactly
like ``config.local.json``.

**An absent dictionary is the normal case, not an error path.** With no file,
``describe("P0420")`` returns ``"P0420"`` and nothing degrades: the code itself
was always the thing the reader produced, and the description is enrichment.
A malformed file logs once and behaves as if absent — a dashboard that stops
showing codes because a hand-edited JSON file lost a comma would be a worse
failure than showing no descriptions.

Where a dictionary is found, most specific last:

  1. ``dtc/generic.json``                      (codes any car can throw)
  2. ``dtc/<profile>.json``                    e.g. ``dtc/lancer_2009.json``
  3. whatever the profile's ``DTC_FILES`` names, in its own order
  4. whatever ``config.local.json``'s ``"dtc"`` key names (a path or a list),
     which replaces 1–3 outright

Later files win per code, so a manufacturer list layers over a generic one.

The schema, entry by entry (the field table in ``docs/DTC_DICTIONARY.md``
quotes this module; this docstring is the authority):

  code       str, required  ``^[PBCU][0-9A-F]{4}$``. The printable key, and the
                            join to what the reader produced.
  desc       str, required  One line, plain English, in the dictionary
                            author's own words. Rejected if empty, if it is
                            the code repeated back, or if it is a placeholder
                            ("Unknown", "No Title", "N/A", …).
  scope      str, required  Which authority defines the code: "generic",
                            "nissan", "mitsubishi", …
  evidence   str, required  One of EVIDENCE, below.
  source     str, required  Where the meaning came from — a URL, a manual
                            section, a named dataset. Never blank: "I don't
                            know" is spelled ``evidence: "unverified"``.
  module     str, optional  The ECU that *defines* the code ("LBC", "TCU").
                            Omit when unknown; never write "unknown".
  desc_long  str, optional  A paragraph, when there is genuinely more to say.
  causes     [str], optional
  aliases    [str], optional  Other printable forms of the same fault.

Four fields are deliberately **absent**, and the reasoning belongs here rather
than in a commit message nobody will find again:

  ecu              belongs to the *sighting*, not to the code. The same code
                   can be reported by different modules, and the responding
                   address is in hand at read time anyway.
  severity         every schema surveyed has this field and not one fills it.
                   Guessing severity on a safety-adjacent EV fault is worse
                   than showing nothing, and an agent asked for it will invent
                   it for all 3000 entries.
  system           derivable from the first letter (P/C/B/U → powertrain /
                   chassis / body / network) — see ``system_of()``. Storing it
                   only invites it to disagree with the code.
  possible_fixes   this is a read-only telemetry dashboard, not a repair
                   manual, and that field is precisely where copyrighted
                   manual text would end up.

The placeholder rejection is not fussiness. The worked example for how this
goes wrong is a public dictionary whose entries carry fourteen fields of which
three are filled: ``"severity": "unknown"``, ``"standard": "Unknown"``,
``"title": "No Title"``, ``sources: []``. A schema with fourteen fields and
three real ones is worse than one with five that are all real, because it
teaches the reader to ignore the fields — and an agent asked to populate a
dictionary will cheerfully emit "unknown" three thousand times and call the
job done.

**The marking rule is enforced here, in code, not by convention.** A
description whose evidence is ``unverified`` or ``community`` is returned with
its tier appended — ``"P0420 — … (unverified)"`` — by every function that
produces display text. An unverified guess rendered in the same typeface as a
service-manual line is actively worse than no description at all: it turns a
known unknown into a confident wrong answer. Because the marker lives in the
string the server produces, a renderer that knows nothing about trouble codes
still cannot show one without it.

Validate a dictionary from the command line (non-zero exit on failure, so it
drops into a hook):

    python dtc.py dtc/lancer_2009.json
"""
import json
import logging
import os
import re

_ROOT = os.path.dirname(os.path.abspath(__file__))
DTC_DIR = os.path.join(_ROOT, "dtc")                 # gitignored; see .gitignore
LOCAL_CONFIG = os.path.join(_ROOT, "config.local.json")

log = logging.getLogger("dtc")

SCHEMA = 1
CODE_RE = re.compile(r"^[PBCU][0-9A-F]{4}$")

#: Evidence tiers, most to least authoritative.
EVIDENCE = ("service-manual", "standard", "observed", "community", "unverified")

#: Tiers that must be visibly marked wherever a description is shown.
MARKED = ("community", "unverified")

REQUIRED = ("code", "desc", "scope", "evidence", "source")
OPTIONAL = ("module", "desc_long", "causes", "aliases")
KNOWN = REQUIRED + OPTIONAL

#: Descriptions that are not descriptions. Compared case-folded and stripped.
PLACEHOLDERS = frozenset((
    "unknown", "no title", "none", "n/a", "na", "tbd", "todo", "?", "-", "--",
    "null", "nil", "no description", "description", "description unknown",
    "not available", "no data", "undefined", "fault", "error", "dtc",
))

SYSTEMS = {"P": "powertrain", "C": "chassis", "B": "body", "U": "network"}

_cache = {}          # (profile, stamp) -> Dictionary
_warned = set()      # paths already complained about, so a bad file logs once


def system_of(code):
    """'powertrain' / 'chassis' / 'body' / 'network' from the code's letter."""
    return SYSTEMS.get(str(code or "")[:1].upper())


# --------------------------------------------------------------- parsing ---

def _is_placeholder(desc, code):
    d = " ".join(str(desc).split()).strip().strip(".").casefold()
    if not d or d in PLACEHOLDERS:
        return True
    return d == str(code or "").casefold()


def parse(data, where="<dict>"):
    """(entries, errors) from an already-parsed dictionary file.

    Never raises. `entries` is {code: entry} for every row that validated;
    `errors` is a list of human-readable strings, one per problem. A file with
    some bad rows still contributes its good ones — a single typo should not
    cost the user the other two hundred entries they researched.
    """
    errors, entries = [], {}
    if not isinstance(data, dict):
        return {}, [f"{where}: top level is {type(data).__name__}, expected an object"]
    schema = data.get("schema", SCHEMA)
    if schema != SCHEMA:
        errors.append(f"{where}: schema {schema!r}, this loader reads {SCHEMA}")
    rows = data.get("codes")
    if not isinstance(rows, list):
        return {}, errors + [f'{where}: "codes" must be a list of entries']

    for i, row in enumerate(rows):
        tag = f"{where}[{i}]"
        if not isinstance(row, dict):
            errors.append(f"{tag}: entry is {type(row).__name__}, expected an object")
            continue
        code = row.get("code")
        if not isinstance(code, str) or not CODE_RE.match(code):
            # An empty "code" is a distinct trap from a missing one: it makes a
            # junk lookup under "" that silently matches nothing.
            errors.append(f'{tag}: code {code!r} is not ^[PBCU][0-9A-F]{{4}}$')
            continue
        tag = f"{where}[{code}]"
        bad = False
        for k in REQUIRED:
            v = row.get(k)
            if not isinstance(v, str) or not v.strip():
                errors.append(f"{tag}: {k} is required and must be a non-empty string")
                bad = True
        if bad:
            continue
        if row["evidence"] not in EVIDENCE:
            errors.append(f"{tag}: evidence {row['evidence']!r} is not one of "
                          + "/".join(EVIDENCE))
            continue
        if _is_placeholder(row["desc"], code):
            errors.append(f"{tag}: desc {row['desc']!r} is a placeholder, not a description")
            continue
        for k in ("causes", "aliases"):
            v = row.get(k)
            if v is not None and not (isinstance(v, list)
                                      and all(isinstance(x, str) for x in v)):
                errors.append(f"{tag}: {k} must be a list of strings")
                bad = True
        if bad:
            continue
        if code in entries:
            errors.append(f"{tag}: duplicate code, later entry wins")
        entry = {k: row[k] for k in KNOWN if row.get(k) not in (None, "", [])}
        entry["marked"] = entry["evidence"] in MARKED
        for k in row:
            if k not in KNOWN:
                errors.append(f"{tag}: unknown field {k!r} (ignored)")
        entries[code] = entry
    return entries, errors


def parse_file(path):
    """(entries, errors) from a path. Missing file → ({}, []): absent is normal."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}, []
    except (OSError, ValueError, UnicodeError) as e:
        return {}, [f"{os.path.basename(path)}: unreadable ({e})"]
    return parse(data, os.path.basename(path))


# -------------------------------------------------------------- lookup ---

class Dictionary:
    """Loaded entries plus where they came from. Lookups are plain dict hits."""

    def __init__(self, entries=None, files=(), errors=()):
        self.entries = entries or {}
        self.files = list(files)
        self.errors = list(errors)

    def __len__(self):
        return len(self.entries)

    def __bool__(self):
        return bool(self.entries)

    def __contains__(self, code):
        return str(code or "").upper() in self.entries

    def get(self, code):
        """The entry for a printable code, or None. Never raises."""
        return self.entries.get(str(code or "").upper())

    def describe(self, code):
        """Display text for one code: 'P0420' or 'P0420 — text (unverified)'.

        The evidence marker is part of the string by design — see the module
        docstring. With no entry the bare code comes back, because an unknown
        code must still be displayable.
        """
        code = str(code or "").strip().upper()
        e = self.get(code)
        if not e:
            return code
        return f"{code} — {e['desc']}{mark(e['evidence'])}"

    def annotate(self, text, sep=" · "):
        """A whitespace-separated code list → the same list, described.

        'none' and anything that is not a printable code passes through
        untouched: the Lancer writes the literal string 'none' when an ECU
        answers with no codes, and that is not a lookup failure.
        """
        if not isinstance(text, str) or not text.strip():
            return text
        parts = text.split()
        if not any(CODE_RE.match(p.upper()) for p in parts):
            return text
        shown = [self.describe(p) if CODE_RE.match(p.upper()) else p for p in parts]
        if shown == parts:
            return text          # nothing was described — leave the text alone
        return sep.join(shown)

    def sightings(self, text):
        """A code list → [{code, desc, evidence, marked, …}], for a richer UI.

        One row per code in the order read, entry fields merged in when the
        dictionary has them. Codes with no entry still get a row.
        """
        rows = []
        for p in str(text or "").split():
            code = p.upper()
            if not CODE_RE.match(code):
                continue
            e = self.get(code)
            row = {"code": code, "desc": None, "evidence": None, "marked": False,
                   "system": system_of(code), "display": self.describe(code)}
            if e:
                row.update({k: v for k, v in e.items() if k != "code"})
            rows.append(row)
        return rows


EMPTY = Dictionary()


def mark(evidence):
    """The visible evidence marker for a tier: ' (unverified)' or ''.

    Enforced here so every caller gets it. See the module docstring on why an
    unmarked guess is worse than no description.
    """
    return f" ({evidence})" if evidence in MARKED else ""


# ------------------------------------------------------------ discovery ---

def _local_paths():
    """Paths named by config.local.json's "dtc" key, or []. Never raises."""
    try:
        with open(LOCAL_CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError, UnicodeError):
        return []
    val = cfg.get("dtc") if isinstance(cfg, dict) else None
    if isinstance(val, dict):
        val = val.get("files")
    if isinstance(val, str):
        val = [val]
    if not isinstance(val, list):
        return []
    return [os.path.join(_ROOT, p) if not os.path.isabs(p) else p
            for p in val if isinstance(p, str) and p]


def candidates(profile=None, vehicle=None):
    """The files that would be layered, least specific first.

    config.local.json, when it names any, replaces the convention outright —
    a user who says where the files are meant it.
    """
    local = _local_paths()
    if local:
        return local
    names = ["generic.json"]
    if profile:
        names.append(f"{profile}.json")
    for n in getattr(vehicle, "DTC_FILES", ()) or ():
        if isinstance(n, str) and n:
            names.append(n if n.endswith(".json") else f"{n}.json")
    out = []
    for n in names:
        p = n if os.path.isabs(n) else os.path.join(DTC_DIR, n)
        if p not in out:
            out.append(p)
    return out


def _stamp(paths):
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append((p, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((p, None, None))
    return tuple(out)


def load(profile=None, vehicle=None, paths=None, force=False):
    """The dictionary for a profile. Cached on the files' mtimes; never raises.

    Call it freely — a cache hit is a stat per candidate file. A malformed
    file logs once per (path, mtime) and then behaves as if it were absent.
    """
    try:
        if vehicle is None and profile is None:
            try:
                from vehicles import get_vehicle
                vehicle = get_vehicle()      # only a fallback: callers pass theirs
            except Exception:
                vehicle = None
        if profile is None:
            profile = getattr(vehicle, "NAME", None)
        files = list(paths) if paths is not None else candidates(profile, vehicle)
        stamp = _stamp(files)
        key = (profile, stamp)
        if not force and key in _cache:
            return _cache[key]

        entries, errors, used = {}, [], []
        for p in files:
            got, errs = parse_file(p)
            if errs:
                warn_key = (p, stamp)
                if warn_key not in _warned:
                    _warned.add(warn_key)
                    log.warning("DTC dictionary %s: %d problem(s); %s",
                                p, len(errs), "; ".join(errs[:3]))
                errors.extend(errs)
            if got:
                entries.update(got)          # later file wins per code
                used.append(p)
        d = Dictionary(entries, used, errors)
        _cache[key] = d
        return d
    except Exception as e:                   # a lookup must never break the page
        log.warning("DTC dictionary unavailable: %s", e)
        return EMPTY


def clear_cache():
    _cache.clear()
    _warned.clear()


# ----------------------------------------------------------- enrichment ---

def dtc_keys(registry):
    """Registry keys that hold trouble codes: entries flagged `"dtc": True`.

    The profile declares them; nothing here knows what a Lancer is.
    """
    return [k for k, s in (registry or {}).items()
            if isinstance(s, dict) and s.get("dtc")]


def enrich(record, registry, dic=None, profile=None, vehicle=None):
    """Add descriptions to a state record's code signals, in place.

    Each flagged key's text is replaced by the described form, and the raw
    string is kept under ``dtc_raw[key]`` so nothing is lost. A structured
    ``dtc_desc[key]`` carries one row per code for any UI that wants more than
    a line of text. With no dictionary the record comes back untouched, which
    is the normal case.

    This runs on the way *out* (``/api/status``), never on the way in: what
    the reader stores stays the raw code, which is the thing that was actually
    read off the car.
    """
    if not isinstance(record, dict):
        return record
    keys = [k for k in dtc_keys(registry) if isinstance(record.get(k), str)]
    if not keys:
        return record
    d = dic if dic is not None else load(profile=profile, vehicle=vehicle)
    raw, desc = {}, {}
    for k in keys:
        text = record[k]
        rows = d.sightings(text)
        if not rows:
            continue
        desc[k] = rows
        shown = d.annotate(text)
        if shown != text:
            raw[k] = text
            record[k] = shown
    if desc:
        record["dtc_desc"] = desc
    if raw:
        record["dtc_raw"] = raw
    return record


# ------------------------------------------------------------ validator ---

def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        d = load()
        print(f"{len(d)} code(s) from {len(d.files)} file(s): "
              + (", ".join(d.files) or "none found"))
        for e in d.errors:
            print("  problem:", e)
        return 1 if d.errors else 0
    bad = 0
    for path in argv:
        if not os.path.exists(path):
            print(f"{path}: no such file")
            bad += 1
            continue
        entries, errors = parse_file(path)
        for e in errors:
            print("ERROR", e)
        tiers = {}
        for e in entries.values():
            tiers[e["evidence"]] = tiers.get(e["evidence"], 0) + 1
        print(f"{path}: {len(entries)} valid entr(ies), {len(errors)} problem(s)"
              + ("  [" + ", ".join(f"{k}:{v}" for k, v in sorted(tiers.items())) + "]"
                 if tiers else ""))
        bad += bool(errors)
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
