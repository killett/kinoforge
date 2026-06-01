"""Opt-in live tests against the real RunPod GraphQL API (Layer N Task 4).

Gated by three env vars:
- ``KINOFORGE_LIVE_TESTS=1`` (global on/off)
- ``RUNPOD_API_KEY=<real key>``
- ``RUNPOD_TERMINATE_KEY=<scoped terminate-only key>``

Optional:
- ``KINOFORGE_SAVE_FIXTURES=1`` — additionally write captured responses to
  ``tests/providers/fixtures/runpod/*.json``.  Pair this flag with a clean
  staging area; the diff is the AC4 review surface.

Cost: ~$0.10-$1.00 per run depending on GPU pick + generation time.  Skipped
silently in CI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.live

if not (
    os.getenv("KINOFORGE_LIVE_TESTS") == "1"
    and os.getenv("RUNPOD_API_KEY")
    and os.getenv("RUNPOD_TERMINATE_KEY")
):
    pytest.skip(
        "live tests require KINOFORGE_LIVE_TESTS=1 + RUNPOD_API_KEY "
        "+ RUNPOD_TERMINATE_KEY",
        allow_module_level=True,
    )


_CONFIG = "examples/configs/runpod-comfyui-wan.yaml"
_INIT_FRAME = "tests/providers/fixtures/runpod/sample_init_frame.png"


def _run_cli(
    args: list[str], cwd: Path | None = None, timeout: int = 900
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m kinoforge`` with the given args, capturing output.

    Args:
        args: CLI argument list (after ``python -m kinoforge``).
        cwd: Working directory for the subprocess.
        timeout: Seconds before the subprocess is forcibly killed.

    Returns:
        A :class:`subprocess.CompletedProcess` with captured stdout/stderr.
    """
    return subprocess.run(
        [sys.executable, "-m", "kinoforge", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def test_runpod_live_e2e_wan_i2v_smoke(tmp_path: Path) -> None:
    """End-to-end live smoke: deploy -> generate -> assert MP4 -> destroy.

    Implements the section 3 control flow from the design.  The cost-safety
    finally-destroy block is guard #3 of 4; see ``examples/configs/runpod-
    comfyui-wan.yaml`` for guards #1, #2, #4.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    # Generate the batch manifest at runtime so the file:// asset path is
    # absolute to THIS contributor's checkout (the committed example manifest
    # uses /workspace/ which only works inside the dev container).
    init_frame_abs = Path(_INIT_FRAME).resolve()
    manifest_path = tmp_path / "smoke-manifest.yaml"
    manifest_path.write_text(
        '- prompt: "A landscape unfurling at dawn"\n'
        "  mode: i2v\n"
        "  run_id: layer-n-smoke\n"
        "  assets:\n"
        "    - kind: image\n"
        "      role: init_image\n"
        f'      ref: "file://{init_frame_abs}"\n'
    )

    # 1. Preconditions — config + init frame present.
    assert Path(_CONFIG).exists()
    assert Path(_INIT_FRAME).exists()

    if os.getenv("KINOFORGE_SAVE_FIXTURES") == "1":
        _capture_fixtures_during_smoke(Path("tests/providers/fixtures/runpod"))

    pod_id: str | None = None
    deploy_started: float = time.monotonic()

    try:
        # 2-3. Deploy (find_offers + create_instance + poll until ready).
        deploy = _run_cli(
            [
                "--state-dir",
                str(state_dir),
                "deploy",
                "--config",
                _CONFIG,
            ],
            timeout=600,
        )
        assert deploy.returncode == 0, (
            f"deploy failed (exit {deploy.returncode}):\n"
            f"stdout:\n{deploy.stdout}\nstderr:\n{deploy.stderr}"
        )

        # Extract pod_id from deploy stdout.
        # CLI prints: "deployed: instance='<id>'"
        for line in deploy.stdout.splitlines():
            if "instance=" in line:
                # strip surrounding quotes if present
                raw = line.split("instance=", 1)[1].strip().strip("'\"")
                if raw and raw != "None":
                    pod_id = raw
                break
        assert pod_id, f"could not parse pod_id from deploy stdout:\n{deploy.stdout}"

        # 4. Generate via batch CLI (provision + submit + download artifact).
        # batch is used because `kinoforge generate` has no --asset flag;
        # the manifest supplies the i2v init_image.
        gen_started = time.monotonic()
        gen = _run_cli(
            [
                "--state-dir",
                str(state_dir),
                "batch",
                "--config",
                _CONFIG,
                "--manifest",
                str(manifest_path),
                "--batch-id",
                "layer-n-smoke-batch",
            ],
            timeout=900,
        )
        gen_duration = time.monotonic() - gen_started
        assert gen.returncode == 0, (
            f"generate failed (exit {gen.returncode}):\n"
            f"stdout:\n{gen.stdout}\nstderr:\n{gen.stderr}"
        )

        # 5. Assertions on the real artifact.
        run_dir = state_dir / "layer-n-smoke-batch" / "layer-n-smoke"
        mp4s = list(run_dir.rglob("*.mp4"))
        assert mp4s, f"no MP4 produced under {run_dir}"
        mp4 = mp4s[0]
        size = mp4.stat().st_size
        assert 100_000 <= size <= 50_000_000, f"MP4 size {size} out of range"
        head = mp4.read_bytes()[:12]
        assert head.startswith(b"\x00\x00\x00") and b"ftyp" in head, (
            f"MP4 magic bytes mismatch: {head!r}"
        )
        assert gen_duration < 900, f"generate too slow: {gen_duration:.1f}s"

        import hashlib

        smoke_meta = {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "git_sha": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            "artifact_path": str(mp4),
            "artifact_size_bytes": size,
            "artifact_sha256": hashlib.sha256(mp4.read_bytes()).hexdigest(),
            "duration_seconds": round(gen_duration, 1),
        }
        last_smoke_path = Path("tests/providers/fixtures/runpod/last_smoke.json")
        last_smoke_path.write_text(
            json.dumps(smoke_meta, indent=2, sort_keys=False) + "\n",
        )

    finally:
        # 6. Destroy — last line of defence before billing leak.
        if pod_id:
            destroy = _run_cli(
                [
                    "--state-dir",
                    str(state_dir),
                    "destroy",
                    "--id",
                    pod_id,
                ],
                timeout=120,
            )
            if destroy.returncode != 0:
                sys.stderr.write(
                    f"\n*** RUNPOD POD {pod_id} NOT CONFIRMED DESTROYED ***\n"
                    f"Manually terminate via the RunPod console or run:\n"
                    f"  curl -X POST https://api.runpod.io/graphql \\\n"
                    f'    -H "Authorization: Bearer $RUNPOD_API_KEY" \\\n'
                    f'    -d \'{{"query":"mutation{{podTerminate('
                    f'input:{{podId:\\"{pod_id}\\"}})}}"}}\'\n'
                )
                raise AssertionError(
                    f"destroy failed (exit {destroy.returncode}):\n"
                    f"stdout:\n{destroy.stdout}\nstderr:\n{destroy.stderr}"
                )

    # 7. Total time check.
    total = time.monotonic() - deploy_started
    assert total < 1800, f"smoke total runtime {total:.1f}s exceeded 30 min"


def _capture_fixtures_during_smoke(out_dir: Path) -> Any:
    """Install a recording HTTP seam under the runpod factory.

    When ``KINOFORGE_SAVE_FIXTURES=1``, called from the live smoke before
    deploy(): re-registers the ``"runpod"`` provider with a factory that
    wraps real http_post/http_get callables in :class:`_RecordingHTTPSeam`.
    The seam's :meth:`flush` is invoked in a teardown finalizer.

    Args:
        out_dir: Directory into which captured fixture JSON files are written.

    Returns:
        The :class:`_RecordingHTTPSeam` instance wired into the provider.
    """
    import atexit

    from kinoforge.core import registry
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.providers.runpod import RunPodProvider, _make_default_http_seams
    from tests.providers.conftest_runpod import _RecordingHTTPSeam

    creds = EnvCredentialProvider()
    authed_post, authed_get = _make_default_http_seams(
        creds.get("RUNPOD_API_KEY"),
    )
    seam = _RecordingHTTPSeam(authed_post, authed_get, out_dir)

    def _factory() -> RunPodProvider:
        return RunPodProvider(
            creds=creds,
            http_post=seam.http_post,
            http_get=seam.http_get,
        )

    registry.register_provider("runpod", _factory)
    atexit.register(seam.flush)
    return seam
