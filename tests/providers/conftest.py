"""Per-package fixtures for provider-layer tests.

The B5a heartbeat substrate fake doubles (FakeRunPodHeartbeatEndpoint,
FakeSkyPilotHeartbeatEndpoint) and their fixtures are defined in the root
tests/conftest.py so they are visible to both tests/core/ and tests/providers/.
This file exists as a package-level conftest for any future provider-specific
fixtures.
"""

from __future__ import annotations
