"""kinoforge doctor subcommand tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.cli._main import main

# Use a non-network model ref so ModelRefReachableCheck does not apply
# (CI doctor smoke needs predictable rc=0 without network reachability
# assumptions). The image is verified against Docker Hub which is
# reliable in CI.
_VALID_CFG = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "local:fake.safetensors"
    kind: base
    target: diffusion_models
compute:
  provider: runpod
  image: "runpod/pytorch:latest"
  mode: pod
  warm_reuse_auto_attach: true
  lifecycle:
    idle_timeout: 15m
    budget: 1.0
    heartbeat_interval_s: 30
"""

_BROKEN_CFG_BAD_CLOUD = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "local:fake.safetensors"
    kind: base
    target: diffusion_models
compute:
  provider: skypilot
  image: "alpine:3"
  mode: pod
  cloud:
    - "nintendo-cloud"
  lifecycle:
    budget: 1.0
"""


def test_doctor_help_prints_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["doctor", "--help"])
    out = capsys.readouterr().out
    assert "--config" in out
    assert "doctor" in out.lower()


def test_doctor_on_valid_cfg_reports_static_pass(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Valid cfg → STATIC pass. NETWORK / PREFLIGHT may return WARN
    inconclusive in CI (no outbound network); both count as ok for
    error-count purposes (rc=0).
    """
    cfg_path = tmp_path / "ok.yaml"
    cfg_path.write_text(_VALID_CFG)
    rc = main(["doctor", "--config", str(cfg_path)])
    out = capsys.readouterr().out
    assert "heartbeat_interval_required" in out
    assert rc == 0


def test_doctor_on_bad_cloud_returns_error_count(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cloud-pin check failure should make rc >= 1 and name the bad entry."""
    cfg_path = tmp_path / "broken.yaml"
    cfg_path.write_text(_BROKEN_CFG_BAD_CLOUD)
    rc = main(["doctor", "--config", str(cfg_path)])
    out = capsys.readouterr().out
    assert rc >= 1
    assert "nintendo-cloud" in out
