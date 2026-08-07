"""Shared fixtures.

chdb runs one embedded server per process, so every test module shares a single
backend. That means seeding must happen exactly once — hence the session-scoped
`seeded` fixture rather than a per-module one. Two modules each calling
`seed_demo` against the same engine would double the telemetry and quietly
change every statistic under test.

Modules that need their own rows use distinct `cut_id`s rather than their own
database.
"""

from __future__ import annotations

import pytest

from crf.db import ChdbBackend, migrate
from crf.pipeline import seed_demo

SEED_VIEWERS = 60
RUN_ID = "test-run"


@pytest.fixture(scope="session")
def backend():
    """Migrated, empty. For tests that load their own fixture rows."""
    be = ChdbBackend()
    migrate(be, verbose=False)
    return be


@pytest.fixture(scope="session")
def seeded(backend):
    """The full closed loop, seeded once for the whole test session:
    cut A screened, notes written, cut B tightened and screened, comments
    generated for both."""
    ctx = seed_demo(
        backend, n_viewers=SEED_VIEWERS, verbose=False, run_id=RUN_ID
    )
    return backend, ctx
