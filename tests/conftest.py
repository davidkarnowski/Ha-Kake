# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared test setup.

Two jobs:

  1. Import paths. The repo is not installed as a package — `reader`, `store`,
     `elm327` and friends are found by adding the repo root and web/ to
     sys.path. Every test file used to repeat that four-line preamble; it
     lives here now, and it runs before any test module is imported.

  2. Fixtures every suite needs: the fixtures directory, a throwaway Store,
     and reader/app globals pointed at tmp_path.

Nothing here touches web/leaf_battery.db or web/battery_state.json. A test
that writes to the real database would be corrupting years of irreplaceable
readings, so `tmp_store` and `isolated_reader` exist to make the safe thing
the easy thing — and since 2026-10-03 the safe thing is also the default:
`_no_real_files` (autouse) points the store's default database and every
reader *_FILE at a per-test temporary folder for every test, whether it asks
or not, and `_real_files_untouched` fails the session if the real database or
settings files changed while the suite ran. (Before that, a test that built
`Store()` or ran the reader without the fixture reached the real web/ files.)
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
for p in (ROOT, os.path.join(ROOT, "web")):
    if p not in sys.path:
        sys.path.insert(0, p)


def fixture(name):
    """Parsed JSON fixture by filename."""
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


WEB = os.path.join(ROOT, "web")
REAL_FILES = ("leaf_battery.db", "tiles.json", "layouts.json", "bookmarks.json",
              "calibration.json", "sim_tiles.json")


def _stamp():
    out = {}
    for name in REAL_FILES:
        try:
            st = os.stat(os.path.join(WEB, name))
            out[name] = (st.st_size, st.st_mtime_ns)
        except FileNotFoundError:
            out[name] = None
    return out


@pytest.fixture(scope="session", autouse=True)
def _real_files_untouched():
    """Tripwire: the owner's real database and settings are the same after the
    suite as before it (size and modification time)."""
    before = _stamp()
    yield
    after = _stamp()
    changed = [n for n in REAL_FILES if before[n] != after[n]]
    assert not changed, f"the test suite modified real files in web/: {changed}"


@pytest.fixture(autouse=True)
def _no_real_files(tmp_path_factory, monkeypatch):
    """Every test runs against temporary files: store.DEFAULT_DB / store.DIR and
    every reader *_FILE path point into a fresh temporary folder."""
    import reader as rd
    import store as st
    d = tmp_path_factory.mktemp("web")
    monkeypatch.setattr(st, "DIR", str(d))
    monkeypatch.setattr(st, "DEFAULT_DB", str(d / os.path.basename(st.DEFAULT_DB)))
    for attr in [a for a in vars(rd) if a.endswith("_FILE") and isinstance(getattr(rd, a), str)]:
        monkeypatch.setattr(rd, attr, str(d / os.path.basename(getattr(rd, attr))))
    yield


@pytest.fixture(scope="session")
def fixtures_dir():
    return FIXTURES


@pytest.fixture
def tmp_store(tmp_path):
    """A Store in tmp_path, for the profile the test has bound."""
    from store import Store
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture
def isolated_reader(tmp_path, monkeypatch):
    """Every file the reader module reads or writes, in tmp_path.

    All of reader's *_FILE paths are redirected — not a hand-kept list, which
    once missed BOOKMARKS_FILE and let a test write the owner's real
    web/bookmarks.json (2026-10-03). A new *_FILE constant is covered by
    construction, and the assertion below fails loudly if one is not."""
    import reader as rd
    legacy = {"STATE_FILE": "state.json", "PAUSE_FILE": "reader.pause", "TILES_FILE": "tiles.json",
              "CALIB_FILE": "calibration.json", "LAYOUTS_FILE": "layouts.json"}   # names tests rely on
    names = sorted(a for a in vars(rd) if a.endswith("_FILE") and isinstance(getattr(rd, a), str))
    for attr in names:
        monkeypatch.setattr(rd, attr, str(tmp_path / legacy.get(attr, os.path.basename(getattr(rd, attr)))))
    assert all(getattr(rd, a).startswith(str(tmp_path)) for a in names)
    yield rd


@pytest.fixture
def leaf_profile():
    """Bind the Leaf profile for the duration of a test, then restore it.

    Profile binding is process-global (reader.set_vehicle → signals.use), so a
    test that switches profiles has to put it back or it poisons the ones after.
    """
    import reader as rd
    rd.set_vehicle("leaf_ze0")
    yield rd.VEHICLE
    rd.set_vehicle("leaf_ze0")


@pytest.fixture
def use_vehicle():
    """Callable: bind a profile for this test only."""
    import reader as rd

    def _use(name):
        rd.set_vehicle(name)
        return rd.VEHICLE

    yield _use
    rd.set_vehicle("leaf_ze0")
