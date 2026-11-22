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
