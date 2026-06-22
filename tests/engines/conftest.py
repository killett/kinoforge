"""Shared pytest helpers for the engines/ test suite.

Layer P fixture-replay helper for ComfyUI offline tests. Mirrors the
Layer N pattern in tests/providers/conftest_runpod.py.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from kinoforge.engines._proxy_retry import RUNPOD_PROXY_POLICY, RetryPolicy

_COMFY_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "comfyui"


def _load_comfy_fixture(name: str) -> dict[str, Any]:
    """Load a captured ComfyUI HTTP response by fixture filename.

    Args:
        name: Fixture filename relative to ``tests/engines/fixtures/comfyui/``
            (e.g. ``"prompt_submit.json"``).

    Returns:
        The ``response`` block of the fixture (the ``_meta`` block is
        captured for forensic value, not surfaced).
    """
    with (_COMFY_FIXTURE_DIR / name).open() as f:
        return dict(json.load(f)["response"])


@pytest.fixture
def fast_policy() -> RetryPolicy:
    """RetryPolicy with three zero-second retries for retry-aware tests.

    Same transient codes + catch classes as RUNPOD_PROXY_POLICY so
    dispatch behavior is identical; only the schedule is compressed so
    tests finish in microseconds.
    """
    return dataclasses.replace(RUNPOD_PROXY_POLICY, backoffs=(0.0, 0.0, 0.0))
