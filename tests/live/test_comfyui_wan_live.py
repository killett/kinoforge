"""Opt-in live smoke: ComfyUI + Wan 2.2 i2v on real RunPod (Layer P).

Produces the first real MP4 from kinoforge end-to-end on real cloud compute.
Runs entirely in-process. Captures ComfyUI HTTP fixtures alongside the
existing Layer N RunPod GraphQL fixtures when KINOFORGE_SAVE_FIXTURES=1.

Gated by four env vars:
- ``KINOFORGE_LIVE_TESTS=1`` (global on/off)
- ``RUNPOD_API_KEY=<real key>``
- ``RUNPOD_TERMINATE_KEY=<scoped terminate-only key>``
- ``HF_TOKEN=<huggingface token>`` (Wan 2.2 weights gated repo)

Optional:
- ``KINOFORGE_SAVE_FIXTURES=1`` — write captured responses.
- ``KINOFORGE_LIVE_KEEP_POD=1`` — skip the destroy step so re-running the
  test reuses the warm pod via tag-discovery. Cost-saving during dev
  iteration; pod's 10-min idle_timeout + selfterm tear it down even if
  the test process dies.

Cost: ~$0.10-$0.30 cold (full provision); ~$0.05 warm (generate only).
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
_TAG_VALUE = "layer-p-smoke"

_MP4_FTYP_PREFIXES: tuple[bytes, ...] = (
    b"ftypisom",
    b"ftypiso5",
    b"ftypiso6",
    b"ftypmp42",
)


def _git_sha() -> str:
    """Return the current HEAD git SHA, or 'unknown' on failure.

    Returns:
        The 40-character hex SHA of HEAD, or ``"unknown"`` if git is
        unavailable or not in a repo.
    """
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def test_runpod_comfyui_wan_live_e2e_smoke() -> None:
    """End-to-end live smoke: deploy ComfyUI on RunPod, generate Wan 2.2 i2v MP4."""
    # Lazy imports — keep module import cheap when test is skipped at collection.
    # _adapters is imported first so every concrete source/engine/provider/store
    # self-registers before the orchestrator tries to resolve refs (HF, etc).
    import kinoforge._adapters  # noqa: F401
    from kinoforge.core.config import load_config
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.interfaces import (
        Artifact,
        ConditioningAsset,
        GenerationRequest,
    )
    from kinoforge.core.orchestrator import generate
    from kinoforge.engines.comfyui import (
        ComfyUIEngine,
    )
    from kinoforge.engines.comfyui import (
        _urllib_get_json as comfy_get,
    )
    from kinoforge.engines.comfyui import (
        _urllib_post_json as comfy_post,
    )
    from kinoforge.outputs.local import LocalOutputSink
    from kinoforge.providers.runpod import RunPodProvider, _make_default_http_seams
    from kinoforge.stores.local import LocalArtifactStore
    from tests.providers.conftest_runpod import (
        _COMFY_DISPATCH,
        _RUNPOD_DISPATCH,
        _RecordingHTTPSeam,
    )

    # --- [phase=setup] -------------------------------------------------
    _log.info("[phase=setup]")
    cfg = load_config(Path("examples/configs/runpod-comfyui-wan.yaml"))
    creds = EnvCredentialProvider()

    runpod_fixtures = Path("tests/providers/fixtures/runpod")
    comfy_fixtures = Path("tests/engines/fixtures/comfyui")
    comfy_fixtures.mkdir(parents=True, exist_ok=True)

    capture = os.getenv("KINOFORGE_SAVE_FIXTURES") == "1"
    keep_pod = os.getenv("KINOFORGE_LIVE_KEEP_POD") == "1"
    _log.info("[phase=setup] capture=%s keep_pod=%s", capture, keep_pod)

    runpod_seam: _RecordingHTTPSeam | None = None
    comfy_seam: _RecordingHTTPSeam | None = None

    if capture:
        api_key = creds.get("RUNPOD_API_KEY") or ""
        authed_post, authed_get = _make_default_http_seams(api_key)
        runpod_seam = _RecordingHTTPSeam(
            authed_post,
            authed_get,
            runpod_fixtures,
            dispatch=_RUNPOD_DISPATCH,
        )
        provider: RunPodProvider = RunPodProvider(
            creds=creds,
            http_post=runpod_seam.http_post,
            http_get=runpod_seam.http_get,
        )
    else:
        provider = RunPodProvider(creds=creds)

    # ModelProfile for the kijai Wan i2v workflow. Both the engine's
    # backend probe AND the warmed JsonProfileCache must declare the
    # same fields — verify() compares cache vs live probe across
    # max_frames/fps/max_resolution/supported_modes and rejects with
    # CapabilityMismatch on any drift (which also tears down the pod).
    # Architectural follow-up: workflow-specific probe should be driven
    # from YAML spec.mode + spec.params, not constructed inline here.
    from kinoforge.core.interfaces import ModelProfile
    from kinoforge.core.profiles import JsonProfileCache

    workflow_profile = ModelProfile(
        name="comfyui",
        max_frames=81,
        fps=24,
        supported_modes={"i2v"},
        max_resolution=(1280, 720),
        supports_native_extension=False,
        supports_joint_audio=False,
    )

    # ComfyUI HTTP — plain (unauthed) seams; pods expose ComfyUI on the
    # public proxy without auth.
    if capture:
        comfy_seam = _RecordingHTTPSeam(
            comfy_post,
            comfy_get,
            comfy_fixtures,
            dispatch=_COMFY_DISPATCH,
        )
        engine: ComfyUIEngine = ComfyUIEngine(
            http_post=comfy_seam.http_post,
            http_get=comfy_seam.http_get,
            probe_profile=workflow_profile,
        )
    else:
        engine = ComfyUIEngine(
            http_post=comfy_post,
            http_get=comfy_get,
            probe_profile=workflow_profile,
        )

    # Layer Q render_provision moves weight downloads to the POD via the
    # rendered curl bootstrap; the provisioner's local download phase
    # would otherwise pull ~21 GB to this container before generate()
    # even gets to wait_for_ready. Flag should be conditionally False on
    # remote-pod engines (Layer Q follow-up); set explicitly here to
    # mirror tools/capture_object_info.py:213.
    engine.requires_local_weights = False

    # Pre-warm the JsonProfileCache so discover() is skipped and
    # generate() takes the cache-hit branch; verify() then sees the
    # same i2v profile from both cache and live backend.inspect.
    profile_cache = JsonProfileCache(
        LocalArtifactStore(root=Path(".kinoforge") / "artifacts")
    )
    profile_cache.warm(cfg.capability_key(), workflow_profile)

    pod_id: str | None = None
    instance: Any = None
    start_time = time.monotonic()
    artifact_path: Path | None = None
    size: int = 0

    try:
        # --- [phase=reuse_check] --------------------------------------
        _log.info("[phase=reuse_check]")
        existing = provider.find_instance_by_tag(_TAG_KEY, _TAG_VALUE)
        if existing is not None:
            pod_id = existing.id
            instance = existing
            _log.info("[phase=reuse_check] warm pod found: %s", pod_id)
        else:
            _log.info("[phase=reuse_check] no warm pod; will create")

        # Cold/warm both flow through orchestrator.generate(): warm path
        # passes the discovered instance; cold path passes instance=None
        # and the orchestrator's _provision_instance_and_build_backend
        # handles find_offers + create_instance (with item #1 offer-retry)
        # + poll_ready. tags= ensures the cold-path pod carries
        # _TAG_KEY=_TAG_VALUE so the NEXT iteration's
        # find_instance_by_tag(...) rediscovers it.

        # --- [phase=provision] ----------------------------------------
        # orchestrator.generate() handles provision via the Layer I
        # provision_state marker — no explicit call here. Marker short-
        # circuits when warm.
        _log.info("[phase=provision] handled by orchestrator.generate()")

        # --- [phase=generate] -----------------------------------------
        _log.info("[phase=generate]")
        init_frame = Path("tests/providers/fixtures/runpod/sample_init_frame.png")
        assert init_frame.exists(), f"init frame missing: {init_frame}"

        init_asset = ConditioningAsset(
            kind="image",
            role="init_image",
            # asset_bytes() dispatches by URI scheme (http/https/file);
            # bare paths raise AssetFetchError("unsupported scheme ''").
            # Use as_uri() to produce a canonical file:///abs/path URI.
            ref=Artifact(filename=init_frame.name, uri=init_frame.resolve().as_uri()),
        )
        request = GenerationRequest(
            # Init-frame-aligned prompt: the canonical
            # prompt-field-realistic.txt is a long-form t2v cinematic about a
            # woman in an alpine meadow, which fights the init image rather
            # than animating it (re-introduces the cat-turns-into-a-woman
            # morph problem that Phase 46 excluded). A short prompt that
            # describes the init image's actual subject — a white long-haired
            # cat — is what exercises i2v's "preserve subject identity"
            # capability cleanly.
            prompt=(
                "A photorealistic close-up of a fluffy white long-haired cat "
                "with bright orange eyes, calmly blinking. Slow gentle "
                "breeze ripples through the fur. Soft bokeh black "
                "background, warm cinematic tones, film grain."
            ),
            mode="i2v",
            assets=[init_asset],
        )

        state_dir = Path(".kinoforge")
        store = LocalArtifactStore(root=state_dir / "artifacts")
        sink = LocalOutputSink(dir=cfg.output.dir)

        artifact, owned_instance = generate(
            cfg,
            request,
            store=store,
            sink=sink,
            provider=provider,
            engine=engine,
            creds=creds,
            state_dir=state_dir,
            instance=instance,  # None on cold start; discovered pod when warm
            tags={  # preserved across orchestrator-managed creates
                "mode": "pod",
                _TAG_KEY: _TAG_VALUE,
                "kinoforge.git_sha": _git_sha(),
            },
            profile_provider=profile_cache,
        )

        # Cold path: orchestrator created the pod inside generate() and now
        # surfaces it back via the return tuple's second element so destroy
        # has a pod_id to act on without resorting to provider tag-discovery
        # (which used to silently miss because RunPod's GraphQL pod schema
        # does not round-trip user tags).
        # Warm path: owned_instance is None because the caller supplied
        # instance=; pod_id stays bound to the warm pod's id from
        # [phase=reuse_check].
        if instance is None and owned_instance is not None:
            pod_id = owned_instance.id
            _log.info(
                "[phase=generate] cold-path pod surfaced by generate(): %s", pod_id
            )

        artifact_path = Path(artifact.uri)
        assert artifact_path.exists(), f"artifact not on disk: {artifact_path}"
        size = artifact_path.stat().st_size
        assert 100 * 1024 <= size <= 50 * 1024 * 1024, (
            f"artifact size {size} out of bounds [100 KB, 50 MB]"
        )
        head = artifact_path.read_bytes()[4:12]
        assert any(head.startswith(p) for p in _MP4_FTYP_PREFIXES), (
            f"artifact ftyp magic mismatch: head={head!r}"
        )
        _log.info(
            "[phase=generate] artifact ok: %s (%d bytes)",
            artifact_path,
            size,
        )

        # Layer O output-sink assertion
        output_dir = cfg.output.dir
        published = list(output_dir.rglob("*.mp4")) if output_dir.exists() else []
        assert published, f"no MP4 published under {output_dir}"
        _log.info("[phase=generate] published: %s", published[0])

        # --- [phase=destroy] ------------------------------------------
        if keep_pod:
            _log.warning(
                "*** POD %s KEPT (KINOFORGE_LIVE_KEEP_POD=1) — re-runs reuse via tag ***",
                pod_id,
            )
        else:
            _log.info("[phase=destroy]")
            if pod_id is not None:
                provider.destroy_instance(pod_id)
            _log.info("[phase=destroy] destroyed normally")

    finally:
        # --- [phase=cleanup_finally] ----------------------------------
        _log.info("[phase=cleanup_finally]")
        for seam in (runpod_seam, comfy_seam):
            if seam is not None:
                try:
                    seam.flush()
                except Exception as exc:  # noqa: BLE001
                    _log.warning("seam.flush() failed: %s", exc)

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

    # --- [phase=record] (green only) ----------------------------------
    _log.info("[phase=record]")
    assert artifact_path is not None, "artifact_path unset — generate did not complete"
    artifact_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    gpu_type: str | None = None
    if instance is not None:
        tags_attr = getattr(instance, "tags", {})
        if isinstance(tags_attr, dict):
            gpu_type = tags_attr.get("gpu_type")

    smoke_meta: dict[str, Any] = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git_sha": _git_sha(),
        "pod_id": pod_id,
        "gpu_type": gpu_type,
        "elapsed_seconds": round(time.monotonic() - start_time, 1),
        "artifact_path": str(artifact_path),
        "artifact_size": size,
        "artifact_sha256": artifact_sha,
        "capability_key": getattr(artifact, "capability_key", None),
    }
    (comfy_fixtures / "last_smoke.json").write_text(
        json.dumps(smoke_meta, indent=2) + "\n"
    )
    _log.info("[phase=record] last_smoke.json written")
