import sys
from pathlib import Path

# Add the project root to sys.path so imports work correctly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import pytest


@pytest.fixture(autouse=True)
def _unlocked_guild_by_default(monkeypatch):
    """
    Default every test to the unlocked (production) guild behaviour.

    Without this, a developer's real DEV_GUILD_ID in .env leaks into the suite
    and blocks every listener test that uses an arbitrary mock guild id — so
    the tests would pass or fail depending on whose machine ran them.
    Tests that care about the lock patch DEV_GUILD_ID explicitly.
    """
    monkeypatch.setattr("services.guild_guard.DEV_GUILD_ID", None, raising=False)


@pytest.fixture(autouse=True)
def _no_live_checklist(monkeypatch, tmp_path):
    """
    Keep a developer's own #trade_checklist edits out of the suite.

    The live checklist file overrides trade_checklist.json when present, so a
    real one on disk would change what every checklist test sees.
    """
    monkeypatch.setattr("cogs.trades.LIVE_CHECKLIST_PATH",
                        tmp_path / "no_live_checklist.json", raising=False)
