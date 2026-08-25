"""Build an InstanceSpec from a config plus an engine's rendered provision.

Extracted from the closure that used to live inside ``deploy_session`` so a
spec can be built without running a deploy — the golden launch-payload
snapshot needs exactly that. Pure: no I/O, no orchestrator state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

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
        tags: Caller tags, merged last so they win over the defaults.
        diagnostic_env: Diagnostic overlay; only used when
            ``cfg.diagnostic_mode`` is set.

    Returns:
        The InstanceSpec to hand to ``provider.create_instance``.
    """
    merged_tags: dict[str, str] = {
        "kinoforge_engine": engine_name,
        "kinoforge_key": key_hash,
    }
    if tags:
        merged_tags.update(tags)
    restart_policy: Literal["always", "never"] = (
        "never" if cfg.diagnostic_mode else "always"
    )
    return InstanceSpec(
        image=rendered.image or image,
        offer=offer,
        ports=tuple(rendered.ports),
        lifecycle=lifecycle,
        tags=merged_tags,
        env=dict(env),
        run_id=run_id,
        # Empty -> None, matching the image_build_script / runtime_provision_script
        # coercion below. rendered.script is a required `str` (never None) on
        # RenderedProvision, so the bare `deploy()` call site — which has no
        # real provision script — must express "none" as "" and rely on this
        # coercion; without it RunPod's `_encode_provision_script` sees a
        # non-None "" and wraps it in a base64/gzip decode-and-run docker
        # command instead of leaving dockerArgs empty.
        provision_script=(rendered.script or None),
        # Modal fast-boot split: bake image_build_script into the image,
        # boot with runtime_provision_script only. Empty -> None so
        # non-splitting engines/providers see no change (RunPod uses the
        # combined provision_script above regardless).
        image_build_script=(rendered.build_script or None),
        runtime_provision_script=(rendered.runtime_script or None),
        run_cmd=rendered.run_cmd,
        diagnostic_env=(
            dict(diagnostic_env) if cfg.diagnostic_mode and diagnostic_env else {}
        ),
        restart_policy=restart_policy,
        cloud_type=(cfg.compute.cloud_type if cfg.compute is not None else "any"),
    )
