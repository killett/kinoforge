"""Live network smoke: every example cfg passes ``kinoforge doctor``.

Gated by ``KINOFORGE_LIVE_TESTS=1``. No pod creation — HEAD probes
only (image registry, HF Hub, GitHub commit URLs). Validates that
none of the example cfgs ship a placeholder image / unreachable
model ref / archived custom-node SHA.

The reference cfg surface lives at ``examples/configs/**/*.yaml``
EXCLUDING ``examples/configs/manifests/`` — that subdir holds batch
manifest YAML (top-level list, not a Config mapping) which is
exercised by ``tests/test_examples.py`` instead.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import kinoforge.providers.runpod  # noqa: F401 — self-register RunPod check
import kinoforge.providers.skypilot  # noqa: F401 — self-register SkyPilot check
import kinoforge.validation.checks  # noqa: F401 — self-register built-ins
from kinoforge.core.config import _parse_cfg_raw
from kinoforge.validation import validate_for_doctor

_LIVE = os.environ.get("KINOFORGE_LIVE_TESTS") == "1"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES = sorted(
    p
    for p in (_REPO_ROOT / "examples/configs").rglob("*.yaml")
    if "manifests" not in p.parts
)


@pytest.mark.skipif(not _LIVE, reason="KINOFORGE_LIVE_TESTS not set")
@pytest.mark.parametrize(
    "cfg_path", _EXAMPLES, ids=lambda p: str(p.relative_to(_REPO_ROOT))
)
def test_example_cfg_passes_doctor(cfg_path: Path) -> None:
    """Every example cfg passes the full doctor validation pass."""
    cfg = _parse_cfg_raw(cfg_path.read_text(), yaml_path=cfg_path)
    report = validate_for_doctor(cfg)
    error_names = [r.name for r in report.errors]
    assert not report.errors, (
        f"{cfg_path.relative_to(_REPO_ROOT)} failed doctor: "
        f"{error_names}\n{report.format()}"
    )
