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
the easy thing.
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
    """reader's on-disk state (state / tiles / calibration / layouts) in tmp_path."""
    import reader as rd
    for attr, name in (("STATE_FILE", "state.json"), ("PAUSE_FILE", "reader.pause"),
                       ("TILES_FILE", "tiles.json"), ("CALIB_FILE", "calibration.json"),
                       ("LAYOUTS_FILE", "layouts.json")):
        monkeypatch.setattr(rd, attr, str(tmp_path / name))
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
