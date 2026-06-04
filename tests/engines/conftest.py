"""Shared pytest helpers for the engines/ test suite.

Layer P fixture-replay helper for ComfyUI offline tests. Mirrors the
Layer N pattern in tests/providers/conftest_runpod.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
