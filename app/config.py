"""Runtime configuration.

The service keeps a single SQLite database file (default ``./data/energy.db``)
and exposes a small set of environment knobs.  ``FAIL_DEVICE`` is a test-only
fault-injection switch: when set, the rebuild of that device always fails
inside the transaction (before commit), so the old intervals stay visible.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str
    fail_device: str | None  # test-only fault injection

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            db_path=os.environ.get("ENERGY_DB_PATH", os.path.join("data", "energy.db")),
            fail_device=os.environ.get("FAIL_DEVICE") or None,
        )
