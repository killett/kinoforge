r"""Capture /object_info from a brief RunPod pod for a kinoforge workflow YAML.

Usage:
    pixi run python tools/capture_object_info.py \\
        --workflow-yaml examples/configs/runpod-comfyui-wan.yaml \\
        [--out tests/fixtures/comfyui/object_info/<hash>.json] \\
        [--env-file .env]

Auto-loads ``.env`` at startup via
:func:`kinoforge.core.dotenv_loader.load_env_file` so RunPod + HF creds
populate ``os.environ`` without manual exporting.

Required creds (auto-loaded from ``.env``):

* ``RUNPOD_API_KEY``
* ``RUNPOD_TERMINATE_KEY``
* ``HF_TOKEN``

Default ``--out`` path is
``tests/fixtures/comfyui/object_info/<pack-stack-hash>.json``, where
``pack-stack-hash`` is derived via :func:`tools._pack_stack.pack_stack_hash`
on the YAML's ``engine.comfyui`` block. Workflows sharing pack stacks
share fixtures.

Layer Q seam: re-uses
:func:`kinoforge.core.orchestrator._provision_instance_and_build_backend`
so this tool walks the exact provisioning path
(``render_provision`` → env-validate → offer-retry → wait for ``ready``
→ ``attach_get_instance`` → ``engine.provision`` → ``engine.backend``)
that the production CLI walks.

Cost: ~$0.10 per capture.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# When invoked as ``python tools/capture_object_info.py``, sys.path[0] is
# ``/<repo>/tools``, so ``from tools._redact import …`` fails. Insert the
# repo root (this file's grandparent) BEFORE any ``tools.*`` import so the
# package resolves. No-op when invoked via ``python -m tools.…`` or when
# the repo root is already on sys.path (e.g. pytest, pixi run).
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools._redact import safe_print  # noqa: E402  sys.path edit above


def _bypass_local_weights_download(engine: object) -> None:
    """Disable the engine's local weight pre-download for capture runs.

    ``ComfyUIEngine.requires_local_weights`` is a class attribute set to
    ``True`` — provisioner.provision then unconditionally downloads every
    declared model artifact onto the controller filesystem BEFORE
    delegating to ``engine.provision()``. For Wan 2.1 i2v that is ~24 GB
    of weights through a ~15 GB-RAM container, which OOM's the host
    (observed twice this session: 3h leaked pod after first OS crash;
    6 GB RSS climb during second attempt before manual SIGKILL).

    The Layer Q ``render_provision`` bootstrap already emits ``curl``
    commands to fetch the same weights on the pod itself. The local copy
    is pure waste for any pod-backed run. Setting the per-instance
    attribute shadows the class default; the rest of the orchestrator
    path is unchanged.

    Note:
        This is the surgical capture-script fix. The architectural fix
        belongs in ``provisioner.provision`` (branch on
        ``instance.provider != 'local'`` to skip local download
        regardless of the engine flag). Tracked as Fix 2 / Layer R.

    Args:
        engine: The just-instantiated ``GenerationEngine`` whose local
            weight download should be skipped for this capture run.
    """
    engine.requires_local_weights = False  # type: ignore[attr-defined]


def _sweep_pods_by_tag(tag_key: str, tag_value: str) -> int:
    """Destroy every RunPod pod whose tags include ``{tag_key: tag_value}``.

    Fallback for orphaned pods when the orchestrator raises out of
    ``_provision_instance_and_build_backend`` before the caller's
    ``try/finally`` over ``provider.destroy_instance(instance.id)`` is
    entered. Catches every shape of mid-flight crash: KeyboardInterrupt,
    network errors during wait_for_ready, controller OS death.

    Sweeps via RunPod REST + Bearer auth using ``RUNPOD_API_KEY`` from
    env. Returns the number of destroyed pods (0 == no leak).

    Args:
        tag_key: The tag key to match (e.g. ``"kinoforge_purpose"``).
        tag_value: The tag value to match (e.g. ``"object_info_capture"``).

    Returns:
        Count of pods destroyed by this sweep.
    """
    import json
    import urllib.error
    import urllib.request

    api_key = os.environ.get("RUNPOD_API_KEY", "")
    if not api_key:
        return 0
    list_req = urllib.request.Request(  # noqa: S310
        "https://rest.runpod.io/v1/pods",
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "kinoforge-capture/1.0",
        },
    )
    try:
        with urllib.request.urlopen(list_req, timeout=30) as resp:  # noqa: S310
            pods = json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return 0
    if not isinstance(pods, list):
        return 0
    destroyed = 0
    for pod in pods:
        if not isinstance(pod, dict):
            continue
        # RunPod REST returns env (incl tags) — for the kinoforge_purpose
        # tag specifically check both `tags` (kinoforge-side) and the
        # generic env block (RunPod tags map to env on create_pod).
        tags = pod.get("tags") or {}
        env = pod.get("env") or {}
        match = (isinstance(tags, dict) and tags.get(tag_key) == tag_value) or (
            isinstance(env, dict) and env.get(tag_key) == tag_value
        )
        if not match:
            continue
        pid = pod.get("id")
        if not isinstance(pid, str):
            continue
        del_req = urllib.request.Request(  # noqa: S310
            f"https://rest.runpod.io/v1/pods/{pid}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "kinoforge-capture/1.0",
            },
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(del_req, timeout=30) as _resp:  # noqa: S310
                destroyed += 1
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            pass
    return destroyed


def _require_env(keys: tuple[str, ...]) -> None:
    """Exit with code 1 (no provisioning) if any *keys* is unset in env.

    Args:
        keys: Names of environment variables required for live execution.
    """
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        safe_print(f"capture_object_info: missing env vars: {missing}")
        sys.exit(1)


def main() -> int:
    """CLI entrypoint. See module docstring for usage.

    Returns:
        ``0`` on success, ``1`` on any failure. The pod is always
        destroyed via the finally clause regardless of return value.
    """
    parser = argparse.ArgumentParser(prog="capture_object_info")
    parser.add_argument(
        "--workflow-yaml",
        required=True,
        type=Path,
        help="Path to a kinoforge workflow YAML.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Output path. Defaults to "
            "tests/fixtures/comfyui/object_info/<pack-stack-hash>.json."
        ),
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Path to .env file to auto-load (default: .env).",
    )
    args = parser.parse_args()

    # Load .env BEFORE env-gate check so secrets in .env populate os.environ.
    # An absent default .env is a silent no-op (per load_env_file contract);
    # an explicitly-passed missing path raises FileNotFoundError. To preserve
    # "missing default is fine, missing explicit is loud" semantics here,
    # only call load_env_file when the resolved path exists.
    from kinoforge.core.dotenv_loader import load_env_file

    if args.env_file.exists():
        load_env_file(args.env_file)

    _require_env(
        (
            "RUNPOD_API_KEY",
            "RUNPOD_TERMINATE_KEY",
            "HF_TOKEN",
        )
    )

    # Deferred imports — only load kinoforge/_adapters after env-gate passes
    # so an env-gate exit does not import provider/engine machinery.
    import kinoforge._adapters  # noqa: F401  registers sources/engines/providers
    from kinoforge.core.config import load_config
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.orchestrator import _provision_instance_and_build_backend
    from kinoforge.stores.local import LocalArtifactStore
    from tools._pack_stack import pack_stack_hash

    cfg = load_config(args.workflow_yaml)
    if cfg.engine.comfyui is None:
        safe_print("capture_object_info: workflow YAML has no engine.comfyui block")
        return 1
    if cfg.compute is None:
        safe_print("capture_object_info: workflow YAML has no compute block")
        return 1

    stack_hash = pack_stack_hash(cfg.engine.comfyui.model_dump())
    out_path: Path = args.out or Path(
        f"tests/fixtures/comfyui/object_info/{stack_hash}.json"
    )

    # Resolve engine + provider via registry. Same path as the production
    # CLI uses; relies on kinoforge._adapters having registered both.
    from kinoforge.core import registry

    engine = registry.get_engine(cfg.engine.kind)()
    _bypass_local_weights_download(engine)
    provider = registry.get_provider(cfg.compute.provider)()
    creds = EnvCredentialProvider()

    # ComfyUI render_provision currently reads ``entry["src"]`` per model,
    # but kinoforge.core.config.ModelEntry serialises to ``ref``. Patch the
    # engine's render_provision in-process to mirror ``ref`` → ``src``
    # before delegating to the original. This keeps capture_object_info
    # self-contained — the parent plan's T1 will fix the schema mismatch
    # upstream and this shim can drop then.
    _orig_render_provision = engine.render_provision

    from kinoforge.core.interfaces import RenderedProvision

    def _render_provision_for_capture(
        cfg_dict: dict[str, object],
    ) -> RenderedProvision:
        # Skip model downloads entirely: /object_info returns the schema
        # for every registered ComfyUI node class — schemas are populated
        # at custom-node module import time, not weight-load time. The
        # 24 GB Wan weight curl in the production bootstrap adds ~15 min
        # of pod time + ~$0.07 per capture for zero schema benefit.
        # Live verification 2026-06-02: ComfyUI + kijai nodes launch
        # cleanly in ~70 s with models=[] (instrumented diagnostic
        # bbkpr6vwy).
        cfg_dict = dict(cfg_dict)
        cfg_dict["models"] = []
        return _orig_render_provision(cfg_dict)

    engine.render_provision = _render_provision_for_capture  # type: ignore[assignment,method-assign]

    # Per-run state under a fresh tempdir — no contamination of
    # .kinoforge/, no marker reuse across captures, no concurrent-run
    # collisions.
    state_dir = Path(tempfile.mkdtemp(prefix="kinoforge-capture-"))
    store = LocalArtifactStore(state_dir / "store")
    key = cfg.capability_key()

    _SWEEP_TAG_KEY = "kinoforge_purpose"
    _SWEEP_TAG_VAL = "object_info_capture"

    try:
        instance, backend = _provision_instance_and_build_backend(
            resolved_engine=engine,
            resolved_provider=provider,
            cfg=cfg,
            run_id="capture",
            key=key,
            creds=creds,
            store=store,
            state_dir=state_dir,
            for_discovery=True,
            tags={_SWEEP_TAG_KEY: _SWEEP_TAG_VAL},
        )
    except BaseException:  # noqa: BLE001 — fallback sweep for ANY mid-flight exception
        # _provision_instance_and_build_backend may create the pod via
        # _create_with_offer_retry BEFORE wait_for_ready completes; if
        # wait_for_ready raises (or is interrupted by SIGINT) the
        # orchestrator's narrow except for ProvisionTimeout / ProvisionFailed
        # destroys via instance.id, but KeyboardInterrupt + other shapes
        # propagate AROUND that handler. By the time control returns here,
        # caller doesn't have instance.id. Sweep by tag instead.
        n = _sweep_pods_by_tag(_SWEEP_TAG_KEY, _SWEEP_TAG_VAL)
        if n:
            safe_print(
                f"capture_object_info: sweep destroyed {n} orphaned pod(s) "
                f"tagged {_SWEEP_TAG_KEY}={_SWEEP_TAG_VAL}"
            )
        raise

    try:
        pod_base_url = backend._base_url  # type: ignore[attr-defined]
        # nosec / noqa explanations:
        # pod_base_url comes from the provider's instance.endpoints["comfyui"]
        # mapping (set by RunPodProvider.create_instance), which the engine
        # rejected unless the proxy URL parsed clean. urlopen here is invoked
        # against an https proxy URL we control end-to-end; no untrusted
        # scheme injection vector.
        request = urllib.request.Request(  # noqa: S310
            f"{pod_base_url}/object_info",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "kinoforge-capture/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:  # noqa: S310
                object_info = json.loads(resp.read())
        except urllib.error.URLError as exc:
            safe_print(f"capture_object_info: GET /object_info failed: {exc}")
            return 1

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                object_info,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        size = out_path.stat().st_size
        print(
            f"wrote {out_path} ({size} bytes, "
            f"{len(object_info)} classes, pack-stack-hash={stack_hash})"
        )
        return 0
    finally:
        try:
            provider.destroy_instance(instance.id)
            safe_print(f"destroyed pod {instance.id}")
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            safe_print(f"capture_object_info: WARNING destroy_instance failed: {exc}")


if __name__ == "__main__":
    sys.exit(main())
