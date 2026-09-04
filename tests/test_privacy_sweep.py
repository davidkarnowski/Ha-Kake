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
    ("mail me at someone@example.com", "e-mail"),
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
