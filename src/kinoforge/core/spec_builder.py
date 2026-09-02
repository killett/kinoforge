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
    from kinoforge.core.interfaces import Lifecycle, RenderedProvision


def build_instance_spec(
    *,
    cfg: Config,
    rendered: RenderedProvision,
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
        ports=tuple(rendered.ports),
        lifecycle=lifecycle,
        tags=merged_tags,
        env=merged_env,
        run_id=run_id,
        # compute-seam S3: the setup/run pair rides through untouched. No
        # coercion and no synthesis — an engine that emits no steps gets `()`
        # and one that starts nothing gets `None`, and a provider is entitled
        # to tell those apart. An engine that renders a `script` but no steps
        # provisions NOTHING, deliberately: a synthesised launch is the guess
        # this stage exists to remove.
        setup_steps=tuple(rendered.setup_steps),
        launch=rendered.launch,
        # compute-seam S1: the portable resource block travels on the spec so
        # a provider reads what to get from one place (SkyPilot's use_spot,
        # S4's declarative selection) instead of re-deriving it from cfg.
        placement=cfg.placement(),
        backend_options=backend_options,
    )
