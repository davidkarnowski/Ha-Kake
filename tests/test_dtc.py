# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for dtc.py — the trouble-code dictionary format, loader and lookup.

No dictionary ships with the project (docs/DTC_DICTIONARY.md says why), so
these tests are the only place a dictionary exists in the repository besides
the synthetic tests/fixtures/dtc_sample.json. Everything here is invented.

Three properties matter more than the rest and each has its own test:

  * **absent is normal** — no file, a missing directory, a malformed file: the
    lookup answers with the bare code and nothing raises;
  * **placeholders are not descriptions** — a schema whose fields are filled
    with "unknown" teaches readers to ignore the fields;
  * **an unverified description is marked wherever it is shown** — the marker
    is produced in dtc.py, not left to a renderer's good manners.
"""
import json
import os

import pytest

from conftest import ROOT, FIXTURES  # noqa: E402  (sys.path is set up there)

import dtc  # noqa: E402

SAMPLE = os.path.join(FIXTURES, "dtc_sample.json")


def write(path, codes, **header):
    doc = {"schema": 1, "vehicle": "test", "codes": codes}
    doc.update(header)
    with open(path, "w") as f:
        json.dump(doc, f)
    return str(path)


def entry(code="P0001", **kw):
    e = {"code": code, "desc": f"A made-up fault for {code}.", "scope": "generic",
         "evidence": "observed", "source": "tests/test_dtc.py"}
    e.update(kw)
    return e


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch, tmp_path):
    """Every test starts with an empty cache and no machine-local config.

    Without the LOCAL_CONFIG override a developer whose config.local.json
    names a real dictionary would see these tests load their car's file.
    """
    monkeypatch.setattr(dtc, "LOCAL_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setattr(dtc, "DTC_DIR", str(tmp_path / "dtc"))
    dtc.clear_cache()
    yield
    dtc.clear_cache()


# ── loading ──────────────────────────────────────────────────────────────

def test_the_shipped_sample_loads_and_is_all_synthetic():
    d = dtc.load(paths=[SAMPLE])
    assert len(d) == 3 and not d.errors
    assert d.files == [SAMPLE]
    # the sample exists to be copied, so every row must be marked as a guess
    assert {e["evidence"] for e in d.entries.values()} == {"unverified"}
    assert d.get("P0AAA")["causes"] == ["An example cause", "Another example cause"]


def test_lookup_is_case_insensitive_and_misses_return_none():
    d = dtc.load(paths=[SAMPLE])
    assert d.get("p0001")["code"] == "P0001"
    assert d.get("P9999") is None
    assert "P0001" in d and "P9999" not in d


def test_load_is_cached_until_the_file_changes(tmp_path):
    p = write(tmp_path / "a.json", [entry("P0001")])
    first = dtc.load(paths=[p])
    assert dtc.load(paths=[p]) is first
    write(tmp_path / "a.json", [entry("P0001"), entry("P0002")])
    os.utime(p, (0, 0))                      # a different mtime is a different file
    assert dtc.load(paths=[p]) is not first


def test_layering_puts_the_specific_file_over_the_generic_one(tmp_path):
    gen = write(tmp_path / "generic.json", [entry("P0001", desc="The generic meaning."),
                                            entry("P0002", desc="Only in the generic file.")])
    make = write(tmp_path / "make.json", [entry("P0001", desc="What this make means by it.",
                                                scope="example-make")])
    d = dtc.load(paths=[gen, make])
    assert d.get("P0001")["desc"] == "What this make means by it."
    assert d.get("P0001")["scope"] == "example-make"
    assert d.get("P0002")["desc"] == "Only in the generic file."   # not lost
    assert d.files == [gen, make]


def test_discovery_finds_generic_and_profile_files_under_the_dtc_dir(tmp_path):
    os.makedirs(tmp_path / "dtc")
    write(tmp_path / "dtc" / "generic.json", [entry("P0001", desc="Generic wording.")])
    write(tmp_path / "dtc" / "lancer_2009.json", [entry("P0001", desc="Profile wording.")])
    assert dtc.candidates("lancer_2009") == [str(tmp_path / "dtc" / "generic.json"),
                                             str(tmp_path / "dtc" / "lancer_2009.json")]
    assert dtc.load(profile="lancer_2009").get("P0001")["desc"] == "Profile wording."


def test_a_profile_can_name_extra_files(tmp_path):
    class Fake:
        NAME = "lancer_2009"
        DTC_FILES = ("mitsubishi",)
    assert dtc.candidates("lancer_2009", Fake) == [
        str(tmp_path / "dtc" / "generic.json"),
        str(tmp_path / "dtc" / "lancer_2009.json"),
        str(tmp_path / "dtc" / "mitsubishi.json")]


def test_config_local_json_replaces_the_convention(tmp_path, monkeypatch):
    p = write(tmp_path / "elsewhere.json", [entry("P0003")])
    cfg = tmp_path / "config.json"
    with open(cfg, "w") as f:
        json.dump({"dtc": [p]}, f)
    monkeypatch.setattr(dtc, "LOCAL_CONFIG", str(cfg))
    assert dtc.candidates("lancer_2009") == [p]
    assert dtc.load(profile="lancer_2009").get("P0003")


# ── absence and damage ───────────────────────────────────────────────────

def test_no_dictionary_at_all_is_normal_and_silent(tmp_path):
    d = dtc.load(profile="leaf_ze0")
    assert len(d) == 0 and d.errors == [] and d.files == []
    assert d.describe("P0A1F") == "P0A1F"           # the code is still displayable
    assert d.annotate("P0A1F P3102") == "P0A1F P3102"   # untouched, not reformatted
    assert d.get("P0A1F") is None


def test_malformed_json_behaves_as_absent_and_complains_once(tmp_path, caplog):
    bad = tmp_path / "broken.json"
    bad.write_text('{"schema": 1, "codes": [ {"code": "P0001",   ')
    d = dtc.load(paths=[str(bad)])
    assert len(d) == 0 and d.errors and d.describe("P0001") == "P0001"
    n = len(caplog.records)
    for _ in range(3):
        dtc.load(paths=[str(bad)], force=True)
    assert len(caplog.records) == n              # logged once per (path, mtime)


@pytest.mark.parametrize("doc", [
    [], "nonsense", {"schema": 1}, {"schema": 1, "codes": {}},
    {"schema": 1, "codes": [None, 3, "P0001"]},
])
def test_structurally_wrong_files_never_raise(tmp_path, doc):
    p = tmp_path / "x.json"
    with open(p, "w") as f:
        json.dump(doc, f)
    d = dtc.load(paths=[str(p)])
    assert len(d) == 0 and d.errors


def test_one_bad_row_does_not_cost_the_good_ones(tmp_path):
    p = write(tmp_path / "a.json", [entry("P0001"), {"code": "nonsense"}, entry("P0002")])
    d = dtc.load(paths=[str(p)])
    assert set(d.entries) == {"P0001", "P0002"} and len(d.errors) == 1


def test_a_future_schema_is_reported_but_still_read(tmp_path):
    p = write(tmp_path / "a.json", [entry("P0001")], schema=2)
    d = dtc.load(paths=[str(p)])
    assert d.errors and d.get("P0001")


# ── validation ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("desc", ["", "   ", "Unknown", "unknown", "No Title", "N/A",
                                  "TBD", "-", "no description", "P0001", "p0001."])
def test_placeholder_descriptions_are_rejected(tmp_path, desc):
    """The lucasveneno lesson: fields filled with "unknown" teach readers to
    ignore the fields, and an agent will emit 3000 of them without blinking."""
    entries, errors = dtc.parse({"schema": 1, "codes": [entry(desc=desc)]})
    assert entries == {} and errors


@pytest.mark.parametrize("row,why", [
    ({"code": "", "desc": "x y.", "scope": "generic", "evidence": "observed", "source": "s"}, "empty code"),
    ({"code": "P001", "desc": "x y.", "scope": "generic", "evidence": "observed", "source": "s"}, "short code"),
    ({"code": "X0001", "desc": "x y.", "scope": "generic", "evidence": "observed", "source": "s"}, "bad letter"),
    ({"code": "P0G01", "desc": "x y.", "scope": "generic", "evidence": "observed", "source": "s"}, "not hex"),
    ({"code": "P0001", "desc": "x y.", "scope": "generic", "evidence": "observed"}, "no source"),
    ({"code": "P0001", "desc": "x y.", "scope": "generic", "evidence": "observed", "source": ""}, "blank source"),
    ({"code": "P0001", "desc": "x y.", "evidence": "observed", "source": "s"}, "no scope"),
    ({"code": "P0001", "desc": "x y.", "scope": "generic", "evidence": "probably", "source": "s"}, "bad tier"),
    ({"code": "P0001", "desc": "x y.", "scope": "generic", "evidence": "observed", "source": "s",
      "causes": "a string"}, "causes not a list"),
])
def test_invalid_entries_are_rejected_with_a_reason(row, why):
    entries, errors = dtc.parse({"schema": 1, "codes": [row]})
    assert entries == {}, why
    assert errors, why


def test_an_empty_code_key_never_becomes_a_lookup():
    entries, _ = dtc.parse({"schema": 1, "codes": [entry(), {"code": "", "desc": "x y.",
                                                            "scope": "g", "evidence": "observed",
                                                            "source": "s"}]})
    assert "" not in entries


def test_duplicates_are_reported_and_the_later_entry_wins():
    entries, errors = dtc.parse({"schema": 1, "codes": [
        entry("P0001", desc="First wording."), entry("P0001", desc="Second wording.")]})
    assert entries["P0001"]["desc"] == "Second wording."
    assert any("duplicate" in e for e in errors)


def test_unknown_fields_are_reported_and_dropped():
    """severity / system / possible_fixes are deliberately not in the schema."""
    entries, errors = dtc.parse({"schema": 1, "codes": [entry(severity="critical")]})
    assert "severity" not in entries["P0001"]
    assert any("severity" in e for e in errors)


def test_optional_fields_survive_and_empty_ones_are_dropped():
    entries, _ = dtc.parse({"schema": 1, "codes": [
        entry(module="TCU", desc_long="More to say.", causes=[], aliases=["P0002"])]})
    e = entries["P0001"]
    assert e["module"] == "TCU" and e["desc_long"] == "More to say."
    assert "causes" not in e and e["aliases"] == ["P0002"]


def test_system_is_derived_not_stored():
    assert dtc.system_of("P0001") == "powertrain"
    assert dtc.system_of("U0001") == "network"
    assert dtc.system_of("B0001") == "body"
    assert dtc.system_of("C0001") == "chassis"
    assert dtc.system_of("") is None and dtc.system_of(None) is None


def test_the_command_line_validator_exits_non_zero_on_a_bad_file(tmp_path, capsys):
    good = write(tmp_path / "good.json", [entry("P0001")])
    bad = write(tmp_path / "bad.json", [entry("P0001", desc="Unknown")])
    assert dtc.main([good]) == 0
    assert dtc.main([bad]) == 1
    assert dtc.main([str(tmp_path / "missing.json")] ) == 1
    assert "placeholder" in capsys.readouterr().out


# ── the marking rule ─────────────────────────────────────────────────────

@pytest.mark.parametrize("tier,marked", [
    ("service-manual", False), ("standard", False), ("observed", False),
    ("community", True), ("unverified", True),
])
def test_weak_evidence_is_marked_wherever_a_description_is_shown(tmp_path, tier, marked):
    p = write(tmp_path / "a.json", [entry("P0001", desc="A described fault.", evidence=tier)])
    d = dtc.load(paths=[str(p)])
    shown = d.describe("P0001")
    assert shown.startswith("P0001 — A described fault.")
    assert (f"({tier})" in shown) is marked
    assert d.get("P0001")["marked"] is marked
    assert d.sightings("P0001")[0]["marked"] is marked
    assert (f"({tier})" in d.annotate("P0001")) is marked
    assert (dtc.mark(tier) != "") is marked


def test_described_and_undescribed_codes_sit_side_by_side(tmp_path):
    p = write(tmp_path / "a.json", [entry("P0171", desc="A made-up mixture fault.",
                                          evidence="service-manual")])
    d = dtc.load(paths=[str(p)])
    assert d.annotate("P0171 P0420") == "P0171 — A made-up mixture fault. · P0420"


def test_annotate_leaves_non_code_text_alone(tmp_path):
    p = write(tmp_path / "a.json", [entry("P0001")])
    d = dtc.load(paths=[str(p)])
    assert d.annotate("none") == "none"          # what the Lancer writes for "no codes"
    assert d.annotate("") == "" and d.annotate(None) is None


def test_sightings_describe_every_code_including_unknown_ones(tmp_path):
    p = write(tmp_path / "a.json", [entry("P0171", evidence="community")])
    rows = dtc.load(paths=[str(p)]).sightings("P0171 P0420 none")
    assert [r["code"] for r in rows] == ["P0171", "P0420"]
    assert rows[0]["marked"] is True and rows[0]["system"] == "powertrain"
    assert rows[1]["desc"] is None and rows[1]["display"] == "P0420"


# ── the Lancer, end to end ───────────────────────────────────────────────

def test_dtc_keys_come_from_the_profile_registry(use_vehicle):
    v = use_vehicle("lancer_2009")
    assert set(dtc.dtc_keys(v.SIGNALS)) == {"dtc_stored", "dtc_pending", "dtc_trans"}
    leaf = __import__("importlib").import_module("vehicles.leaf_ze0")
    assert dtc.dtc_keys(leaf.SIGNALS) == []      # the Leaf reads no codes yet, by decision


def test_enrich_describes_the_lancer_signals_and_keeps_the_raw_codes(tmp_path, use_vehicle):
    v = use_vehicle("lancer_2009")
    p = write(tmp_path / "a.json", [
        entry("P0171", desc="A made-up lean-mixture fault.", evidence="service-manual"),
        entry("P2195", desc="A made-up sensor-stuck fault.", evidence="unverified")])
    d = dtc.load(paths=[str(p)])
    rec = {"dtc_stored": "P0171 P2195 P1234", "dtc_pending": "none", "soc": 61.5}
    dtc.enrich(rec, v.SIGNALS, dic=d)
    assert rec["dtc_stored"] == ("P0171 — A made-up lean-mixture fault. · "
                                 "P2195 — A made-up sensor-stuck fault. (unverified) · P1234")
    assert rec["dtc_raw"]["dtc_stored"] == "P0171 P2195 P1234"
    assert rec["dtc_pending"] == "none" and "dtc_pending" not in rec.get("dtc_raw", {})
    assert [r["code"] for r in rec["dtc_desc"]["dtc_stored"]] == ["P0171", "P2195", "P1234"]
    assert rec["soc"] == 61.5


def test_enrich_without_a_dictionary_changes_nothing(use_vehicle):
    v = use_vehicle("lancer_2009")
    rec = {"dtc_stored": "P0171 P2195", "soc": 61.5}
    dtc.enrich(rec, v.SIGNALS, dic=dtc.EMPTY)
    assert rec["dtc_stored"] == "P0171 P2195" and "dtc_raw" not in rec
    assert [r["display"] for r in rec["dtc_desc"]["dtc_stored"]] == ["P0171", "P2195"]


def test_enrich_survives_junk():
    assert dtc.enrich(None, {}) is None
    rec = {"dtc_stored": 17}
    dtc.enrich(rec, {"dtc_stored": {"dtc": True}}, dic=dtc.EMPTY)
    assert rec == {"dtc_stored": 17}


def test_api_status_serves_described_codes(tmp_path, monkeypatch, use_vehicle):
    """The enrichment happens on the way out of /api/status, not in the store."""
    import app as webapp
    import reader as rd
    import signals as sig
    use_vehicle("lancer_2009")
    os.makedirs(tmp_path / "dtc")
    write(tmp_path / "dtc" / "lancer_2009.json",
          [entry("P0171", desc="A made-up lean-mixture fault.", evidence="community")])
    dtc.clear_cache()
    state = tmp_path / "state.json"
    with open(state, "w") as f:
        json.dump({"status": "ok", "dtc_stored": "P0171 P0420", "dtc_trans": "none"}, f)
    monkeypatch.setattr(webapp, "STATE_FILE", str(state))
    monkeypatch.setattr(webapp, "DEMO", None)
    webapp.app.config["TESTING"] = True
    with webapp.app.test_client() as c:
        body = c.get("/api/status").get_json()
    assert body["dtc_stored"] == "P0171 — A made-up lean-mixture fault. (community) · P0420"
    assert body["dtc_raw"]["dtc_stored"] == "P0171 P0420"
    assert body["dtc_desc"]["dtc_stored"][0]["evidence"] == "community"
    assert body["dtc_trans"] == "none"
    assert rd.VEHICLE.NAME == "lancer_2009" and "dtc_stored" in sig.SIGNALS


def test_api_status_is_unchanged_when_no_dictionary_exists(tmp_path, monkeypatch, use_vehicle):
    import app as webapp
    use_vehicle("lancer_2009")
    state = tmp_path / "state.json"
    with open(state, "w") as f:
        json.dump({"status": "ok", "dtc_stored": "P0171 P0420"}, f)
    monkeypatch.setattr(webapp, "STATE_FILE", str(state))
    monkeypatch.setattr(webapp, "DEMO", None)
    with webapp.app.test_client() as c:
        body = c.get("/api/status").get_json()
    assert body["dtc_stored"] == "P0171 P0420" and "dtc_raw" not in body


# ── the repository property ──────────────────────────────────────────────

def test_no_dictionary_is_tracked_in_this_repository():
    """`dtc/` is gitignored and the only tracked dictionary is the sample."""
    import subprocess
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True).stdout.split()
    assert not [f for f in tracked if f.startswith("dtc/")]
    assert "tests/fixtures/dtc_sample.json" in tracked
