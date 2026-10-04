# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for scripts/privacy_sweep.py.

The sweep is the last thing standing between a private garage and a public
repo, so its rule table and its history scanner get the same treatment as a
decoder: fixtures in, expectations out, no hardware and no network.

Note the history tests build a throwaway git repo in a tmp_path rather than
scanning this one — a test that asserted things about *our* history would
break the moment the release squash lands.
"""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

from conftest import ROOT  # noqa: E402  (sys.path is set up there)

SWEEP = os.path.join(ROOT, "scripts", "privacy_sweep.py")

spec = importlib.util.spec_from_file_location("privacy_sweep", SWEEP)
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)


def names(findings):
    return {f[1] for f in findings}


# ------------------------------------------------------------ rule table ---

@pytest.mark.parametrize("text,rule", [
    ("path = /Users/somebody/Projects/x", "home path"),
    ("ADDR = \"0A2B71BF-7812-999C-8905-B1D28E23973A\"", "device UUID"),
    ("see https://claude.ai/code/session_01AbCdEf", "claude session"),
    ("port = /dev/tty.usbserial-1420", "serial port"),
    ("mail me at someone@mailhost.invalid", "e-mail"),
    ("adapter at 192.168.1.44", "IPv4"),
])
def test_rules_fire(text, rule):
    found = []
    ps.scan_text("f", text, found)
    assert rule in names(found)


def test_privacy_ok_marker_exempts_the_line():
    found = []
    ps.scan_text("f", "contact someone@example.com  privacy-ok", found)
    assert found == []


@pytest.mark.parametrize("text,rule,sev", [
    ('char = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"', "uuid (lower)", "WARN"),
    ("adapter AA:BB:CC:11:22:33 paired", "BLE/MAC address", "ERROR"),
    ("captures/JN1AZ0CP5BT012345_drive.jsonl", "VIN", "ERROR"),
    (r"dir C:\Users\alice\leaf", "home path", "ERROR"),
    ("https://claude.ai/chat/0f2c1a7e-1234-4cde-9abc-0123456789ab", "claude session", "ERROR"),
    ("port /dev/cu.usbmodem2071385A4E5B1", "serial port", "ERROR"),
    ("port /dev/cu.wchusbserial14A2B3C", "serial port", "ERROR"),
    ("token github_pat_11ABCDEFG0123456789abcdefghij", "secret-looking", "ERROR"),
    ("key AIzaSyA-0123456789abcdefghijklmnopqrstuv", "secret-looking", "ERROR"),
    ("key sk-proj-abcdefghijklmnopqrstuvwxyz0123", "secret-looking", "ERROR"),
    ('password = "hunter2hunter2"', "secret assignment", "WARN"),
])
def test_broadened_rules_fire_at_their_severity(text, rule, sev):
    found = []
    ps.scan_text("f", text, found)
    assert (sev, rule) in {(f[0], f[1]) for f in found}, found


@pytest.mark.parametrize("text", [
    'BLE_FFE1 = "0000ffe1-0000-1000-8000-00805f9b34fb"',       # the Bluetooth base UUID
    "write to jane@example.com or ops@lab.example.org",
    "Co-Authored-By: Claude <noreply@anthropic.com>",
    "binds 127.0.0.1 and documents 192.0.2.10, 198.51.100.7, 203.0.113.9",
    "a float 0.15915494309189535 is not a VIN",
])
def test_public_by_construction_is_not_an_error(text):
    found = []
    ps.scan_text("f", text, found)
    assert [f for f in found if f[0] == "ERROR"] == [], found


def test_a_short_generic_serial_port_name_is_only_a_warning():
    found = []
    ps.scan_text("f", "DEFAULT_PORT = '/dev/tty.usbserial-0001'", found)
    assert {(f[0], f[1]) for f in found} == {("WARN", "serial port")}


def test_a_bare_marker_silences_warnings_but_not_errors():
    found = []
    ps.scan_text("f", "contact someone@mailhost.invalid, user dk  privacy-ok", found)
    assert {(f[0], f[1]) for f in found} == {("ERROR", "e-mail")}
    found = []
    ps.scan_text("f", "contact someone@mailhost.invalid, user dk  <!-- privacy-ok:e-mail -->", found)
    assert found == []


def test_an_encoded_secret_is_found():
    import base64
    blob = base64.b64encode(b"path=/Users/alice/Projects/leaf secret").decode()
    found = []
    ps.scan_text("f", f'CONFIG = "{blob}"', found)
    assert "home path (base64)" in names(found)
    found = []
    ps.scan_text("f", "x = " + base64.b64encode(b"\x00\x01binary noise\xff" * 4).decode(), found)
    assert found == []


def test_a_hostile_megabyte_line_is_scanned_quickly():
    import time
    t0 = time.monotonic()
    ps.scan_text("f", "A" * 1_000_000 + " " + "a1:" * 200_000 + " " + "/" * 500_000, [])
    assert time.monotonic() - t0 < 2.0


def test_paths_private_folders_and_databases_are_refused():
    found = []
    heads = {"data/blob.bin": b"SQLite format 3\x00", "web/static/app.js": b"// js"}
    ps.scan_paths(["research/notes.md", "web/leaf_battery.db", "data/blob.bin", "web/static/app.js",
                   "captures/JN1AZ0CP5BT012345_drive.jsonl"], found, heads.get)
    got = {(f[1], f[2]) for f in found}
    assert ("private folder", "research/notes.md") in got
    assert ("database file", "web/leaf_battery.db") in got
    assert ("database file", "data/blob.bin") in got
    assert ("VIN (path)", "captures/JN1AZ0CP5BT012345_drive.jsonl") in got
    assert not any(p == "web/static/app.js" for _, p in got)


def test_clean_text_is_clean():
    found = []
    ps.scan_text("f", "SOC 62.4%, pack 390.2 V, four temps in C and F\n", found)
    assert found == []


def test_log_skip_covers_authorship_only():
    """Session URLs must be reported in commit messages; author identity is
    expected there and stays skipped (see LOG_SKIP)."""
    assert "claude session" not in ps.LOG_SKIP
    assert set(ps.LOG_SKIP) == {"e-mail", "username"}


# --------------------------------------------------------- history scan ----

def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def leaky_repo(tmp_path, monkeypatch):
    """A repo whose HEAD is clean but whose history carries a device UUID."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    f = repo / "adapter.py"
    f.write_text('ADDR = "0A2B71BF-7812-999C-8905-B1D28E23973A"\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "import")
    f.write_text('ADDR = os.environ["ADAPTER"]\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "scrub the adapter id")
    monkeypatch.setattr(ps, "ROOT", str(repo))
    return repo


def test_head_is_clean_but_history_is_not(leaky_repo):
    head = []
    for rel in ps.tracked_files():
        ps.scan_text(rel, (leaky_repo / rel).read_text(), head)
    assert head == [], "HEAD should be clean — that is the whole point"

    rows, images, head_blobs = ps.scan_history()
    assert any(r[1] == "device UUID" for r in rows), "history scan missed the leak"
    hit = next(r for r in rows if r[1] == "device UUID")
    assert hit[3] == "adapter.py"
    # path still tracked, but this blob is not the one at HEAD
    assert head_blobs.get("adapter.py") not in {r[2] for r in rows}


def test_history_scan_finds_nothing_in_a_clean_repo(tmp_path, monkeypatch):
    repo = tmp_path / "clean"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    (repo / "ok.py").write_text("SOC = 62.4\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "clean")
    monkeypatch.setattr(ps, "ROOT", str(repo))
    rows, images, _ = ps.scan_history()
    assert rows == [] and images == []


def test_replaced_image_is_warned_about(tmp_path, monkeypatch):
    """The unblurred-screenshot case: same path at HEAD, different bytes before."""
    repo = tmp_path / "img"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    shot = repo / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"original" * 8)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "screenshot")
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"blurred!" * 8)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "blur the screenshot")
    monkeypatch.setattr(ps, "ROOT", str(repo))
    _, images, _ = ps.scan_history()
    assert [i[0] for i in images] == ["shot.png"]
    assert images[0][1] == 2                      # two distinct versions


# ------------------------------------------------------------- end to end --

def test_plain_run_still_passes_on_this_tree():
    r = subprocess.run([sys.executable, SWEEP], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
    assert "privacy sweep OK" in r.stdout


# ------------------------------------------------- trouble-code dictionaries --
#
# The project ships the DTC format and none of the data (docs/DTC_DICTIONARY.md).
# `dtc/` is gitignored, but an ignore rule is a habit; these tests are the part
# that makes "we won't commit it" a property of the repository.

DICT = json.dumps({
    "schema": 1, "vehicle": "example", "codes": [
        {"code": "P0001", "desc": "An invented fault, for this test only.",
         "scope": "generic", "evidence": "unverified", "source": "tests"}]})


def test_a_tracked_dictionary_is_an_error_by_path():
    found = []
    ps.scan_dtc("dtc/lancer_2009.json", DICT, found)
    assert [f[1] for f in found] == ["dtc dictionary"] and found[0][0] == "ERROR"


def test_a_tracked_dictionary_is_an_error_wherever_it_is_put():
    """Renaming it out of dtc/ must not get it past the sweep."""
    found = []
    ps.scan_dtc("docs/codes.json", DICT, found)
    assert [f[1] for f in found] == ["dtc dictionary"]


def test_the_synthetic_sample_is_allowed():
    found = []
    ps.scan_dtc("tests/fixtures/dtc_sample.json",
                open(os.path.join(ROOT, "tests", "fixtures", "dtc_sample.json")).read(), found)
    assert found == []


@pytest.mark.parametrize("path,text", [
    ("web/tiles.json", json.dumps({"tiles": [{"id": "u_dtc"}]})),
    ("scripts/x.py", 'rows = data["codes"]  # "evidence" is a field name\n'),
    ("docs/DTC_DICTIONARY.md", 'A row carries "codes", "evidence" and "desc".\n'),
    ("x.json", "not json at all {"),
])
def test_ordinary_files_do_not_trip_the_dictionary_rule(path, text):
    found = []
    ps.scan_dtc(path, text, found)
    assert found == []


def test_the_sweep_fails_on_a_dictionary_added_to_a_repo(tmp_path):
    """End to end: a dictionary force-added to a tracked path fails the sweep."""
    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, capture_output=True, text=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.invalid")
    run("git", "config", "user.name", "T")
    (repo / "dtc").mkdir()
    (repo / "dtc" / "leaf_ze0.json").write_text(DICT)
    run("git", "add", "-f", "dtc/leaf_ze0.json")
    run("git", "commit", "-qm", "add a dictionary")
    # the sweep resolves ROOT from its own location, so point it at the throwaway repo
    out = subprocess.run([sys.executable, "-c",
                          "import importlib.util,sys;"
                          f"spec=importlib.util.spec_from_file_location('ps',{SWEEP!r});"
                          "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
                          f"m.ROOT={str(repo)!r};sys.exit(m.main())"],
                         capture_output=True, text=True)
    assert out.returncode == 1, out.stdout
    assert "dtc dictionary" in out.stdout and "privacy sweep FAILED" in out.stdout
