"""Build a Block-Sparse-Attention wheel on RunPod + upload to GitHub release.

One-shot ops tool for T7.5.b/c of the FlashVSR plan
(``docs/superpowers/plans/2026-07-01-flashvsr-video-upscaling.md``).

Design: fire a single RunPod A6000 on
``runpod/pytorch:2.8.0-py3.11-cuda12.8.1-cudnn-devel-ubuntu22.04``; its
provision script builds ``block-sparse-attn`` at BSA commit ``3453bbb1``
against SM80+SM86+SM89+SM90 with ``MAX_JOBS=4``; the built ``.whl`` is
uploaded to the GH release ``bsa-cu128-torch2.8-v1`` on
``killett/kinoforge-artifacts`` via the uploads API using ``$GH_TOKEN``;
this driver polls the release-assets endpoint every 60 s from outside
and destroys the pod as soon as an asset lands. Pod is destroyed on any
exit path (success, timeout, keyboard interrupt).

Budget: ``~$1``, hard ceiling ``$2`` via ``lifecycle.budget_usd``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import replace

from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.interfaces import (
    HardwareRequirements,
    InstanceSpec,
    Lifecycle,
)
from kinoforge.providers.runpod import RunPodProvider

_GH_OWNER = "killett"
_GH_REPO = "kinoforge-artifacts"
_GH_TAG = "bsa-cu128-torch2.8-v1"
_BASE_IMAGE = "runpod/pytorch:2.8.0-py3.11-cuda12.8.1-cudnn-devel-ubuntu22.04"
_BSA_COMMIT = "3453bbb1"
_POLL_INTERVAL_S = 60
_MAX_WAIT_S = 45 * 60


def _log(msg: str) -> None:
    """Print a timestamped log line to stderr."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _get_release_id(gh_token: str) -> int:
    """Look up the numeric release ID for our pinned tag."""
    url = f"https://api.github.com/repos/{_GH_OWNER}/{_GH_REPO}/releases/tags/{_GH_TAG}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return int(json.load(resp)["id"])


def _list_release_assets(gh_token: str, release_id: int) -> list[dict[str, object]]:
    """Return the release's current asset list (empty until wheel lands)."""
    url = (
        f"https://api.github.com/repos/{_GH_OWNER}/{_GH_REPO}"
        f"/releases/{release_id}/assets"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data: list[dict[str, object]] = json.load(resp)
        return data


def _build_provision_script(release_id: int) -> str:
    """Render the on-pod build + upload script.

    The pod's docker entrypoint decodes + runs this via
    ``bash -c "echo $KINOFORGE_PROVISION_SCRIPT | base64 -d > /tmp/p.sh
    && chmod +x /tmp/p.sh && bash /tmp/p.sh"`` — see
    :meth:`RunPodProvider._create_pod`.

    Ends with ``exit`` so the container terminates naturally on success.
    On failure (any ``set -e`` trap), the container also exits and the
    self-terminator sweeps the pod on its next tick.
    """
    upload_url = (
        f"https://uploads.github.com/repos/{_GH_OWNER}/{_GH_REPO}"
        f"/releases/{release_id}/assets"
    )
    return f"""set -euo pipefail
echo "=== BSA WHEEL BUILDER ==="
echo "pod=$(hostname) date=$(date -Is)"
pip install --quiet packaging ninja
export TORCH_CUDA_ARCH_LIST="8.0;8.6;8.9;9.0"
export MAX_JOBS=4
echo "=== CLONING BSA @{_BSA_COMMIT} ==="
git clone --depth 100 https://github.com/mit-han-lab/Block-Sparse-Attention.git /tmp/bsa
cd /tmp/bsa
git checkout {_BSA_COMMIT}
echo "=== BUILDING WHEEL (this is the long step) ==="
mkdir -p /tmp/whl
time pip wheel --no-deps --no-build-isolation --wheel-dir /tmp/whl .
WHEEL=$(ls /tmp/whl/*.whl | head -1)
NAME=$(basename "$WHEEL")
SHA=$(sha256sum "$WHEEL" | cut -d' ' -f1)
SIZE=$(stat -c%s "$WHEEL")
echo "=== WHEEL_MANIFEST ==="
echo "NAME=$NAME"
echo "SIZE=$SIZE"
echo "SHA256=$SHA"
echo "=== UPLOADING to {_GH_OWNER}/{_GH_REPO}@{_GH_TAG} ==="
curl -sSL --fail-with-body -X POST \\
  -H "Authorization: Bearer $GH_TOKEN" \\
  -H "Content-Type: application/octet-stream" \\
  -H "Accept: application/vnd.github+json" \\
  --data-binary @"$WHEEL" \\
  "{upload_url}?name=$NAME"
echo ""
echo "=== UPLOAD_DONE ==="
exit 0
"""


def _spec_for_build(release_id: int, gh_token: str) -> InstanceSpec:
    """Build the InstanceSpec for the one-shot builder pod."""
    return InstanceSpec(
        image=_BASE_IMAGE,
        volume_gb=0,  # no persistent storage — wheel goes straight to GH.
        lifecycle=Lifecycle(
            idle_timeout_s=45 * 60,
            job_timeout_s=45 * 60,
            time_buffer_s=1 * 60,
            max_lifetime_s=45 * 60,
            budget_usd=2.0,
            boot_timeout_s=5 * 60,
        ),
        env={"GH_TOKEN": gh_token},
        tags={"mode": "pod", "kinoforge_purpose": "bsa-wheel-build"},
        run_id="bsa-wheel-builder",
        provision_script=_build_provision_script(release_id),
        restart_policy="never",  # single-shot; don't auto-relaunch on exit.
    )


def _pick_offer(provider: RunPodProvider) -> object:
    """Ask the provider for a matching A6000 (or 4090) offer, cheapest first."""
    reqs = HardwareRequirements(
        min_vram_gb=48,
        min_cuda="12.4",
        max_usd_per_hr=1.5,
        gpu_preference=(
            "NVIDIA RTX A6000",
            "NVIDIA GeForce RTX 4090",
            "NVIDIA L40",
            "NVIDIA L40S",
        ),
        disk_gb=100,
    )
    offers = provider.find_offers(reqs)
    if not offers:
        raise SystemExit(
            "no offers matched — check RunPod capacity or widen gpu_preference"
        )
    return offers[0]


def _poll_until_uploaded_or_timeout(
    provider: RunPodProvider,
    pod_id: str,
    gh_token: str,
    release_id: int,
    deadline_s: float,
) -> str | None:
    """Poll every 60 s until the wheel asset appears in the GH release.

    Returns the wheel filename on success, ``None`` on timeout / pod death.
    """
    while time.monotonic() < deadline_s:
        try:
            assets = _list_release_assets(gh_token, release_id)
        except Exception as exc:  # noqa: BLE001 — best-effort poll
            _log(f"asset-list probe failed (retrying): {exc}")
            assets = []
        wheels = [a for a in assets if str(a.get("name", "")).endswith(".whl")]
        if wheels:
            return str(wheels[0]["name"])

        try:
            inst = provider.get_instance(pod_id)
            probe = provider.probe_runtime(pod_id)
        except Exception as exc:  # noqa: BLE001
            _log(f"pod-status probe failed (retrying): {exc}")
            time.sleep(_POLL_INTERVAL_S)
            continue

        gpu = 0
        cpu = 0
        if probe is not None:
            gpu = int(probe.gpu_util_pct or 0)
            cpu = int(probe.cpu_pct or 0)
        elapsed_m = int((time.monotonic() - (deadline_s - _MAX_WAIT_S)) / 60)
        _log(
            f"pod={pod_id} status={inst.status} gpu={gpu}% cpu={cpu}% "
            f"elapsed={elapsed_m}m assets={len(assets)}"
        )
        if inst.status in ("stopped", "terminated"):
            _log("pod terminated without wheel upload — build likely failed")
            return None
        time.sleep(_POLL_INTERVAL_S)
    _log(f"deadline exceeded ({_MAX_WAIT_S // 60}m) — giving up")
    return None


def main() -> int:
    """Entry point — spin builder pod, poll, destroy on any exit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render provision script + print, don't create a pod.",
    )
    args = parser.parse_args()

    gh_token = os.environ.get("GH_TOKEN") or ""
    if not gh_token:
        raise SystemExit("GH_TOKEN not set in env (need `repo` scope for uploads)")

    release_id = _get_release_id(gh_token)
    _log(f"release_id={release_id} tag={_GH_TAG}")

    if args.dry_run:
        script = _build_provision_script(release_id)
        print(script)
        _log(f"dry-run: script len={len(script)} bytes")
        return 0

    _log("running preflight ...")
    r = subprocess.run(
        ["pixi", "run", "preflight"], check=False, capture_output=True, text=True
    )
    if r.returncode != 0:
        raise SystemExit(f"preflight FAILED:\n{r.stdout}\n{r.stderr}")
    _log("preflight PASS")

    provider = RunPodProvider(creds=EnvCredentialProvider())
    offer = _pick_offer(provider)
    _log(
        f"picked offer gpu={offer.gpu_type!r} "  # type: ignore[attr-defined]
        f"rate=${offer.cost_rate_usd_per_hr:.2f}/hr"  # type: ignore[attr-defined]
    )

    spec = replace(_spec_for_build(release_id, gh_token), offer=offer)  # type: ignore[arg-type]
    _log(f"provision script len={len(spec.provision_script or '')} bytes")

    inst = provider.create_instance(spec)
    _log(f"pod created id={inst.id} status={inst.status}")
    pod_id = inst.id
    deadline = time.monotonic() + _MAX_WAIT_S
    try:
        wheel_name = _poll_until_uploaded_or_timeout(
            provider, pod_id, gh_token, release_id, deadline
        )
    finally:
        _log(f"destroying pod {pod_id} ...")
        try:
            provider.destroy_instance(pod_id)
        except Exception as exc:  # noqa: BLE001
            _log(f"destroy_instance raised (may already be gone): {exc}")

    if wheel_name is None:
        return 2

    _log(f"SUCCESS wheel_name={wheel_name}")
    print(wheel_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
