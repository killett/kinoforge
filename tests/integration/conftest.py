"""Shared fixtures for the warm-reuse LoRA integration tests.

The ``RedactionRegistry`` is a process-wide singleton; without an
inter-test wipe, refs registered by an earlier test (e.g. the overlap
scenario) leak into a later test's ledger reads as tokenised
placeholders, breaking equality assertions on persisted refs.
"""

from __future__ import annotations

import pytest

from kinoforge.core.redaction import RedactionRegistry


@pytest.fixture(autouse=True)
def _clear_redaction_registry() -> None:
    """Reset the singleton registry before every test in this directory."""
    RedactionRegistry.instance().clear_session()
