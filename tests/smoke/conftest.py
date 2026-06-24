"""Shared fixtures for all smoke tiers.

Wipes the singleton RedactionRegistry between tests so refs registered
by an earlier test don't leak into later assertions (same pattern as
``tests/integration/conftest.py``).

Also flips pytest's ``log_cli`` ON at ``WARNING`` for any test collected
under ``tests/smoke/``.  Pre-2026-06-24 the destroy-on-teardown sweep
logged failed reaps at ``_log.warning(...)`` and pytest's default
``log_cli=False`` swallowed every one of them — the operator saw the
test pass while the pod kept burning money.  Scoping the flip to this
conftest keeps unit-test runs (``pytest tests/cli/``) noise-free.
"""

from __future__ import annotations

import pytest

from kinoforge.core.redaction import RedactionRegistry


def pytest_configure(config: pytest.Config) -> None:
    """Enable WARNING-level console log capture for smoke-tier tests.

    Defence-in-depth against the 2026-06-23 destroy-on-teardown bug
    where ``runpod_lifecycle.destroy_all_active_pods``'s
    ``_log.warning`` calls reached no operator surface.  Even if
    ``teardown_pod_or_raise``'s post-condition probe regresses, a
    WARNING log on the failed reap will still hit the terminal.
    """
    config.option.log_cli = True
    if not getattr(config.option, "log_cli_level", None):
        config.option.log_cli_level = "WARNING"


@pytest.fixture(autouse=True)
def _clear_redaction_registry() -> None:
    RedactionRegistry.instance().clear_session()
