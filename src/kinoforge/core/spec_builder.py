"""Build an InstanceSpec from a config plus an engine's rendered provision.

Extracted from the closure that used to live inside ``deploy_session`` so a
spec can be built without running a deploy — the golden launch-payload
snapshot needs exactly that. Pure: no I/O, no orchestrator state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kinoforge.core.interfaces import InstanceSpec

if TYPE_CHECKING:
    from kinoforge.core.config import Config
    from kinoforge.core.interfaces import Lifecycle, Offer, RenderedProvision


def build_instance_spec(
    *,
    cfg: Config,
    rendered: RenderedProvision,
    offer: Offer | None,
    engine_name: str,
    key_hash: str,
    image: str,
    lifecycle: Lifecycle,
    env: dict[str, str],
    run_id: str,
    tags: dict[str, str] | None = None,
    diagnostic_env: dict[str, str] | None = None,
) -> InstanceSpec:
    """Map a config + rendered provision onto an InstanceSpec.

    Args:
        cfg: The loaded config.
        rendered: The engine's rendered provision payload.
        offer: The chosen offer, or None on paths that do not select one.
        engine_name: Registry name of the resolved engine (goes into tags).
        key_hash: Capability-key hash (goes into tags; warm-reuse matches it).
        image: Fallback image when ``rendered.image`` is empty.
        lifecycle: Effective lifecycle guardrails.
        env: Credential-resolved environment for the instance.
        run_id: Run identifier.
        tags: Caller tags, merged over ``cfg.compute.tags`` so a
            per-invocation label (CLI flag, grid cell) wins over the config's
            static one. Neither can overwrite ``kinoforge_engine`` /
            ``kinoforge_key``.
        diagnostic_env: Diagnostic overlay; only used when
            ``cfg.diagnostic_mode`` is set. Merged into ``env`` via
            ``setdefault`` so an operator-supplied value always wins.

    Returns:
        The InstanceSpec to hand to ``provider.create_instance``.
    """
    merged_env = dict(env)
    if cfg.diagnostic_mode and diagnostic_env:
        for key, value in diagnostic_env.items():
            merged_env.setdefault(key, value)
    # Precedence reads top-to-bottom: kinoforge's own keys, then the cfg's
    # static labels, then the caller's per-invocation ones.
    merged_tags: dict[str, str] = {
        "kinoforge_engine": engine_name,
        "kinoforge_key": key_hash,
    }
    if cfg.compute is not None and cfg.compute.tags:
        merged_tags.update(cfg.compute.tags)
    if tags:
        merged_tags.update(tags)
    # kinoforge-owned keys are re-asserted last: a cfg or caller that sets
    # kinoforge_key would break warm-reuse matching, which keys off it.
    merged_tags["kinoforge_engine"] = engine_name
    merged_tags["kinoforge_key"] = key_hash
    # RunPod branches on this tag (providers/runpod/__init__.py, create_instance).
    # Before S2 nothing wrote it, so `compute.mode: serverless` silently took
    # the pod branch — the S1 whole-branch review proved the payload was
    # identical either way. setdefault, not assignment: RunPod's own internal
    # re-create call passes an explicit mode tag and must keep winning.
    if cfg.compute is not None:
        merged_tags.setdefault("mode", cfg.compute.mode)
    backend_options: dict[str, dict[str, Any]] = {
        name: dict(opts)
        for name, opts in (
            cfg.compute.backend_options if cfg.compute is not None else {}
        ).items()
    }
    if cfg.diagnostic_mode:
        # C28 A3: diagnostic mode wants a failed boot to leave the container
        # dead so its logs survive. That is a RunPod-shaped knob, so the
        # portable flag is translated into the RunPod namespace here rather
        # than riding a vendor field on the portable InstanceSpec. Overwrites
        # any operator value on purpose — --diagnostic-mode is the more
        # specific, per-invocation intent.
        backend_options.setdefault("runpod", {})["restart_policy"] = "never"
    return InstanceSpec(
        image=rendered.image or image,
        offer=offer,
        ports=tuple(rendered.ports),
        lifecycle=lifecycle,
        tags=merged_tags,
        env=merged_env,
        run_id=run_id,
        # Empty -> None, matching the image_build_script / runtime_provision_script
        # coercion below. rendered.script is a required `str` (never None) on
        # RenderedProvision, so the bare `deploy()` call site — which has no
        # real provision script — must express "none" as "" and rely on this
        # coercion; without it RunPod's `_encode_provision_script` sees a
        # non-None "" and wraps it in a base64/gzip decode-and-run docker
        # command instead of leaving dockerArgs empty.
        # This also changes `deploy_session`, whose pre-S1 closure passed
        # `rendered.script` through verbatim: an engine that rendered `script=""`
        # used to get that same empty decode-and-run wrapper and now gets an
        # empty dockerArgs. Inert today — all three engines (comfyui, diffusers,
        # fake) render a non-empty script on that path, so no shipped config
        # reaches the delta, which is why the golden ratchet shows no movement.
        # It is called out because it landed in the one commit window the
        # ratchet could not cover; a future engine that legitimately renders an
        # empty script gets the (correct) empty-dockerArgs behaviour, not the
        # old no-op wrapper.
        provision_script=(rendered.script or None),
        # Modal fast-boot split: bake image_build_script into the image,
        # boot with runtime_provision_script only. Empty -> None so
        # non-splitting engines/providers see no change (RunPod uses the
        # combined provision_script above regardless).
        image_build_script=(rendered.build_script or None),
        runtime_provision_script=(rendered.runtime_script or None),
        run_cmd=rendered.run_cmd,
        # compute-seam S1: the portable resource block travels on the spec so
        # a provider reads what to get from one place (SkyPilot's use_spot,
        # S4's declarative selection) instead of re-deriving it from cfg.
        placement=cfg.placement(),
        backend_options=backend_options,
    )
