"""Tests for `preflight --check-hosted` Bearer-cred env gate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_PREFLIGHT = Path(__file__).resolve().parents[2] / "tools" / "preflight.py"
_HOSTED_VARS = (
    "REPLICATE_API_TOKEN",
    "RUNWAYML_API_SECRET",
    "LUMAAI_API_KEY",
    "FAL_KEY",
)


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """Run the preflight script in a clean env; never inherit the host env."""
    return subprocess.run(  # noqa: S603
        [sys.executable, str(_PREFLIGHT), *args],
        env={**env},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_preflight_without_check_hosted_ignores_missing_keys(tmp_path: Path) -> None:
    """Baseline: the default invocation never complains about hosted creds.

    Bug caught: a future refactor that runs the hosted gate
    unconditionally would silently break every `pixi run preflight` call
    that doesn't have the four Bearer keys set (i.e. every non-Layer-4
    workflow).
    """
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    for var in _HOSTED_VARS:
        env.pop(var, None)
    result = _run(env)  # no --check-hosted
    # Pre-existing checks may legitimately fail (no .env, no runpod creds,
    # dirty tree); we only assert that no hosted-cred complaint surfaces.
    for var in _HOSTED_VARS:
        assert var not in result.stderr


def test_preflight_check_hosted_passes_when_all_four_set(tmp_path: Path) -> None:
    """All four creds present → hosted gate emits no missing-var stderr lines.

    Bug caught: a typo in the env-var list (e.g. `REPLICATE_TOKEN` instead
    of `REPLICATE_API_TOKEN`) would mark a present cred as missing and
    block live smokes.
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "REPLICATE_API_TOKEN": "x",
        "RUNWAYML_API_SECRET": "x",
        "LUMAAI_API_KEY": "x",
        "FAL_KEY": "x",
    }
    result = _run(env, "--check-hosted")
    for var in _HOSTED_VARS:
        assert f"missing {var}" not in result.stderr.lower()


def test_preflight_check_hosted_fails_on_missing_replicate(tmp_path: Path) -> None:
    """Missing REPLICATE_API_TOKEN → non-zero exit with its name on stderr.

    Bug caught: silent miss — preflight returns success even though the
    Replicate live smoke is about to spend money on an unauthenticated
    request that will 401.
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "RUNWAYML_API_SECRET": "x",
        "LUMAAI_API_KEY": "x",
        "FAL_KEY": "x",
    }
    result = _run(env, "--check-hosted")
    assert result.returncode != 0
    assert "REPLICATE_API_TOKEN" in result.stderr


def test_preflight_check_hosted_fails_on_missing_runway(tmp_path: Path) -> None:
    """Missing RUNWAYML_API_SECRET → non-zero exit with its name on stderr.

    Bug caught: same as above but for Runway; ensures every provider in
    the four-key set is checked symmetrically.
    """
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "REPLICATE_API_TOKEN": "x",
        "LUMAAI_API_KEY": "x",
        "FAL_KEY": "x",
    }
    result = _run(env, "--check-hosted")
    assert result.returncode != 0
    assert "RUNWAYML_API_SECRET" in result.stderr
