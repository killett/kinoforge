"""Opt-in live smoke: ComfyUI + Wan 2.1 14B t2v on real RunPod, two
back-to-back generations on ONE pod (in-process warm-reuse).

Why this test exists at all (vs the existing i2v live smoke):

* The i2v smoke proved Wan + ComfyUI + RunPod end-to-end with an
  image-conditioned workflow. This smoke covers the t2v variant —
  text-only conditioning, no CLIP-vision, no init-image asset wiring —
  and adds a second generation against the SAME live pod to exercise
  the warm-reuse path that CLI users would expect once
  ``LifecycleManager.warm_reuse_or_create`` is exposed at the CLI
  surface (PROGRESS B3 / B4).

* Today the warm path is only reachable from inside a single Python
  process: callers pass the ``Instance`` returned by
  ``orchestrator.generate()`` back in as the next call's ``instance=``
  kwarg, which skips ``create_instance`` and the boot poll. The CLI
  re-creates a fresh pod on every invocation — that gap is tracked
  separately and is out of scope here.

Gated by four env vars (same as the i2v smoke):
- ``KINOFORGE_LIVE_TESTS=1`` (global on/off)
- ``RUNPOD_API_KEY=<real key>``
- ``RUNPOD_TERMINATE_KEY=<scoped terminate-only key>``
- ``HF_TOKEN=<huggingface token>`` (Wan weights gated repo)

Optional:
- ``KINOFORGE_LIVE_KEEP_POD=1`` — skip the destroy step (dev iteration).

Cost ceiling: ~$0.50 worst case (4090 @ ~$0.40/hr × 60-75 min wall for
cold boot + 2 generations). Realistic: ~$0.10-$0.30 on A5000 @ $0.16/hr.
Skipped silently in CI.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.live

if not (
    os.getenv("KINOFORGE_LIVE_TESTS") == "1"
    and os.getenv("RUNPOD_API_KEY")
    and os.getenv("RUNPOD_TERMINATE_KEY")
    and os.getenv("HF_TOKEN")
):
    pytest.skip(
        "live tests require KINOFORGE_LIVE_TESTS=1 + RUNPOD_API_KEY "
        "+ RUNPOD_TERMINATE_KEY + HF_TOKEN",
        allow_module_level=True,
    )

_log = logging.getLogger(__name__)

_TAG_KEY = "kinoforge.layer"
_TAG_VALUE = "wan-t2v-smoke"

_MP4_FTYP_PREFIXES: tuple[bytes, ...] = (
    b"ftypisom",
    b"ftypiso5",
    b"ftypiso6",
    b"ftypmp42",
)

_PROMPT_REALISTIC_PATH = Path("/workspace/examples/configs/prompts/field-realistic.txt")
_PROMPT_DREAMLIKE_PATH = Path("/workspace/examples/configs/prompts/field-dreamlike.txt")


def _git_sha() -> str:
    """Return the current HEAD git SHA, or 'unknown' on failure."""
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _assert_mp4(path: Path) -> int:
    """Assert a path is a valid MP4 in the expected size band; return byte size."""
    assert path.exists(), f"artifact not on disk: {path}"
    size = path.stat().st_size
    assert 100 * 1024 <= size <= 50 * 1024 * 1024, (
        f"artifact size {size} out of bounds [100 KB, 50 MB]: {path}"
    )
    head = path.read_bytes()[4:12]
    assert any(head.startswith(p) for p in _MP4_FTYP_PREFIXES), (
        f"artifact ftyp magic mismatch: head={head!r} path={path}"
    )
    return size


def test_runpod_comfyui_wan_t2v_warm_reuse_two_prompts() -> None:
    """Cold-boot one pod, run TWO t2v generations on it, verify both.

    Sequence:
      1. Cold path — ``generate()`` with ``instance=None`` creates the
         pod, provisions Wan t2v weights, runs the first prompt
         (realistic), returns ``(artifact_1, owned_instance)``.
      2. Warm path — ``generate()`` with ``instance=owned_instance``
         skips ``create_instance`` + boot poll, reuses the same backend
         to run the second prompt (dreamlike), returns
         ``(artifact_2, None)`` (warm caller-owned path).
      3. Both artifacts validated as MP4s with distinct content
         (different prompt → different bytes).
      4. ``provider.destroy_instance(pod_id)`` in finally — single pod
         throughout, single termination at the end.
    """
    # Lazy imports — keep module import cheap when test is skipped at collection.
    import kinoforge._adapters  # noqa: F401
    from kinoforge.core.config import load_config
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.interfaces import GenerationRequest, ModelProfile
    from kinoforge.core.orchestrator import generate
    from kinoforge.core.profiles import JsonProfileCache
    from kinoforge.engines.comfyui import ComfyUIEngine
    from kinoforge.engines.comfyui import _urllib_get_json as comfy_get
    from kinoforge.engines.comfyui import _urllib_post_json as comfy_post
    from kinoforge.outputs.local import LocalOutputSink
    from kinoforge.providers.runpod import RunPodProvider
    from kinoforge.stores.local import LocalArtifactStore

    # --- [phase=setup] -------------------------------------------------
    _log.info("[phase=setup]")
    cfg = load_config(Path("examples/configs/runpod-comfyui-wan-t2v.yaml"))
    creds = EnvCredentialProvider()
    keep_pod = os.getenv("KINOFORGE_LIVE_KEEP_POD") == "1"
    _log.info("[phase=setup] keep_pod=%s", keep_pod)

    assert _PROMPT_REALISTIC_PATH.exists(), (
        f"prompt file missing: {_PROMPT_REALISTIC_PATH}"
    )
    assert _PROMPT_DREAMLIKE_PATH.exists(), (
        f"prompt file missing: {_PROMPT_DREAMLIKE_PATH}"
    )
    prompt_realistic = _PROMPT_REALISTIC_PATH.read_text().strip()
    prompt_dreamlike = _PROMPT_DREAMLIKE_PATH.read_text().strip()
    assert prompt_realistic and prompt_dreamlike, (
        "prompts must be non-empty after strip()"
    )
    assert prompt_realistic != prompt_dreamlike, (
        "prompts must differ — comparison is the whole point of warm-reuse here"
    )

    provider = RunPodProvider(creds=creds)

    # ModelProfile for the Wan t2v workflow. Mirrors the i2v live test's
    # pattern: pre-warm the JsonProfileCache so generate() takes the
    # cache-hit branch and the engine probe matches.
    workflow_profile = ModelProfile(
        name="comfyui",
        max_frames=81,
        fps=16,
        supported_modes={"t2v"},
        max_resolution=(1280, 720),
        supports_native_extension=False,
        supports_joint_audio=False,
    )

    engine = ComfyUIEngine(
        http_post=comfy_post,
        http_get=comfy_get,
        probe_profile=workflow_profile,
    )
    # ComfyUIEngine now declares requires_local_weights=False at the class
    # level (Layer Q's pod-side curl bootstrap is the actual provisioning
    # path), so the per-instance override that earlier live tests carried
    # is no longer needed. Kept here as a comment for archeology — the flag
    # behavior is regression-tested by
    # tests/engines/test_comfyui_no_local_weight_dl.py.

    state_dir = Path(".kinoforge")
    store = LocalArtifactStore(root=state_dir / "artifacts")
    profile_cache = JsonProfileCache(store)
    profile_cache.warm(cfg.capability_key(), workflow_profile)
    sink = LocalOutputSink(dir=cfg.output.dir)

    pod_id: str | None = None
    owned_instance: Any = None
    start_time = time.monotonic()
    artifact_1_path: Path | None = None
    artifact_2_path: Path | None = None
    artifact_1_sha: str | None = None
    artifact_2_sha: str | None = None
    size_1: int = 0
    size_2: int = 0
    cold_elapsed: float = 0.0
    warm_elapsed: float = 0.0

    try:
        # --- [phase=generate_cold] (first prompt, fresh pod) ----------
        _log.info("[phase=generate_cold] realistic prompt, cold-boot path")
        request_1 = GenerationRequest(
            prompt=prompt_realistic,
            mode="t2v",
            assets=[],
        )
        cold_start = time.monotonic()
        artifact_1, owned_instance = generate(
            cfg,
            request_1,
            store=store,
            sink=sink,
            provider=provider,
            engine=engine,
            creds=creds,
            state_dir=state_dir,
            run_id="t2v-realistic",
            instance=None,  # cold path — orchestrator creates pod
            tags={
                "mode": "pod",
                _TAG_KEY: _TAG_VALUE,
                "kinoforge.git_sha": _git_sha(),
                "kinoforge.prompt": "realistic",
            },
            profile_provider=profile_cache,
        )
        cold_elapsed = time.monotonic() - cold_start
        assert owned_instance is not None, (
            "cold path must return the orchestrator-created instance"
        )
        pod_id = owned_instance.id
        artifact_1_path = Path(artifact_1.uri)
        size_1 = _assert_mp4(artifact_1_path)
        artifact_1_sha = hashlib.sha256(artifact_1_path.read_bytes()).hexdigest()
        _log.info(
            "[phase=generate_cold] pod=%s artifact=%s size=%d cold_elapsed=%.1fs",
            pod_id,
            artifact_1_path,
            size_1,
            cold_elapsed,
        )

        # --- [phase=generate_warm] (second prompt, same pod) ----------
        # The whole point of this test: pass owned_instance back in so
        # the orchestrator's deploy_session skips create_instance and
        # the boot poll. The same backend + provisioned weights serve
        # the second generation.
        _log.info("[phase=generate_warm] dreamlike prompt, warm-reuse path")
        request_2 = GenerationRequest(
            prompt=prompt_dreamlike,
            mode="t2v",
            assets=[],
        )
        warm_start = time.monotonic()
        artifact_2, second_owned = generate(
            cfg,
            request_2,
            store=store,
            sink=sink,
            provider=provider,
            engine=engine,
            creds=creds,
            state_dir=state_dir,
            run_id="t2v-dreamlike",
            instance=owned_instance,  # warm path — reuses pod
            tags=None,  # tags ignored when instance= supplied
            profile_provider=profile_cache,
        )
        warm_elapsed = time.monotonic() - warm_start
        assert second_owned is None, (
            "warm path must return None for owned_instance — caller still owns it"
        )
        artifact_2_path = Path(artifact_2.uri)
        size_2 = _assert_mp4(artifact_2_path)
        artifact_2_sha = hashlib.sha256(artifact_2_path.read_bytes()).hexdigest()
        _log.info(
            "[phase=generate_warm] artifact=%s size=%d warm_elapsed=%.1fs",
            artifact_2_path,
            size_2,
            warm_elapsed,
        )

        # --- [phase=invariants] --------------------------------------
        assert artifact_1_path != artifact_2_path, (
            "two generations must publish to distinct output paths"
        )
        assert artifact_1_sha != artifact_2_sha, (
            "two different prompts must produce different bytes — "
            "same SHA suggests the second generation got the first's output"
        )
        # Warm should be materially faster than cold (no boot, no
        # weight downloads). Allow generous margin — Wan inference
        # dominates and varies — but warm < cold proves no re-provision.
        assert warm_elapsed < cold_elapsed, (
            f"warm_elapsed={warm_elapsed:.1f}s >= cold_elapsed={cold_elapsed:.1f}s — "
            "warm path appears to have re-provisioned"
        )

        # --- [phase=destroy] -----------------------------------------
        if keep_pod:
            _log.warning("*** POD %s KEPT (KINOFORGE_LIVE_KEEP_POD=1) ***", pod_id)
        else:
            _log.info("[phase=destroy] pod=%s", pod_id)
            provider.destroy_instance(pod_id)
            _log.info("[phase=destroy] destroyed normally")

    finally:
        # --- [phase=cleanup_finally] ---------------------------------
        # Belt-and-braces: destroy the pod even on test failure, unless
        # explicit keep flag set. Same pattern as the i2v live test.
        if pod_id is not None and not keep_pod:
            try:
                provider.destroy_instance(pod_id)
                _log.info("pod %s confirmed destroyed (finally path)", pod_id)
            except Exception as exc:  # noqa: BLE001
                import sys

                sys.stderr.write(
                    f"\n*** RUNPOD POD {pod_id} NOT CONFIRMED DESTROYED ***\n"
                    f"Error: {exc}\n"
                    f"Manually terminate via the RunPod console.\n"
                )
                raise

    # --- [phase=record] (green only) ---------------------------------
    _log.info("[phase=record]")
    assert artifact_1_path is not None and artifact_2_path is not None
    smoke_meta: dict[str, Any] = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git_sha": _git_sha(),
        "pod_id": pod_id,
        "total_elapsed_seconds": round(time.monotonic() - start_time, 1),
        "cold_elapsed_seconds": round(cold_elapsed, 1),
        "warm_elapsed_seconds": round(warm_elapsed, 1),
        "artifacts": [
            {
                "prompt_label": "realistic",
                "path": str(artifact_1_path),
                "size": size_1,
                "sha256": artifact_1_sha,
            },
            {
                "prompt_label": "dreamlike",
                "path": str(artifact_2_path),
                "size": size_2,
                "sha256": artifact_2_sha,
            },
        ],
    }
    fixtures_dir = Path("tests/engines/fixtures/comfyui")
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    (fixtures_dir / "last_t2v_smoke.json").write_text(
        json.dumps(smoke_meta, indent=2) + "\n"
    )
    _log.info("[phase=record] last_t2v_smoke.json written")
