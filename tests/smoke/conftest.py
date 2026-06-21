"""Shared fixtures for all smoke tiers.

Wipes the singleton RedactionRegistry between tests so refs registered
by an earlier test don't leak into later assertions (same pattern as
``tests/integration/conftest.py``).
"""

from __future__ import annotations

import pytest

from kinoforge.core.redaction import RedactionRegistry


@pytest.fixture(autouse=True)
def _clear_redaction_registry() -> None:
    RedactionRegistry.instance().clear_session()
