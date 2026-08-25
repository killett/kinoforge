"""Regenerate the golden launch payloads under tests/providers/golden/launch_payloads/.

Run deliberately, never from a test: a regenerated golden is a reviewed act::

    pixi run python tools/snapshot_launch_payloads.py

Every provider is driven through an injected fake, so nothing here touches a
network, a credential store, or a cloud account. The captured payload is the
*wire* payload — RunPod's GraphQL ``variables.input`` dict, Modal's
``ModalAppRequest``, SkyPilot's ``(task_config, launch_kwargs)`` pair — not the
:class:`~kinoforge.core.interfaces.InstanceSpec` that produced it. That is the
whole point: the S1..S5 compute-seam rework changes the SHAPE of
``ComputeConfig`` and ``InstanceSpec``, and must not change what any provider
actually sends.

Determinism contract (a golden that diffs on every run is worse than none):

* ``run_id`` is pinned to :data:`GOLDEN_RUN_ID`.
* ``time.time`` is frozen at :data:`FROZEN_EPOCH` for the duration of every
  capture (SkyPilot stamps the watchdog deadline off the wall clock; Modal
  stamps ``Instance.created_at``).
* Credentials are never read. Every value the engine declares in
  ``env_required``, and every value a provider asks its credential provider
  for, is the synthetic :data:`STUB_SECRET`.
* Offer selection is NOT captured — offers come from a live catalog call.
  Each config gets the frozen synthetic offer built by :func:`_catalog_offer`,
  so the golden pins the spec -> payload mapping. Offer selection itself is
  S4's subject.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

if TYPE_CHECKING:
    from collections.abc import Iterator

    from kinoforge.core.config import Config
    from kinoforge.core.interfaces import InstanceSpec, Offer

GOLDEN_DIR = Path("tests/providers/golden/launch_payloads")
CONFIG_DIR = Path("examples/configs")
FROZEN_EPOCH = 1756000000.0
GOLDEN_RUN_ID = "golden-run"
GOLDEN_KEY_HASH = "golden-key"
# Not a credential: the repo-wide synthetic placeholder. The `deadbeef` marker
# is a strong suppressor for tools/scan_secrets.py, so it can never be mistaken
# for a real value by the pre-commit scanner or the tracked-tree guard.
STUB_SECRET = "kinoforge-prod-deadbeef"  # noqa: S105

#: Configs deliberately outside the ratchet, mapped to the reason. An entry
#: here is a reviewed decision, not a silent hole: the golden test reads this
#: mapping and reports the config + reason when it skips one.
EXCLUDED_CONFIGS: dict[str, str] = {
    "runpod-diffusers-serverless.yaml": (
        "does not render today, with or without this snapshot: its engine "
        "block has `diffusers:` commented out, so DiffusersEngine."
        "render_provision raises AttributeError on `diffusers_cfg.get('pip')` "
        "(engines/diffusers/__init__.py, `pip_deps` line). The config is "
        "un-deployable as shipped, so there is no wire payload to freeze. "
        "Delete this entry once the config grows an `engine.diffusers` block."
    ),
}


class _StubCreds:
    """Credential provider that answers every lookup with :data:`STUB_SECRET`.

    Stands in for ``EnvCredentialProvider`` so a capture never reads ``.env``
    or the process environment. Answering (rather than returning ``None``)
    matters: RunPod only injects ``RUNPOD_TERMINATE_KEY`` into the pod env
    when the lookup succeeds, so a ``None``-returning stub would drop that key
    from the golden and stop the ratchet from noticing if a later stage
    dropped it for real.
    """

    def get(self, name: str) -> str | None:
        """Return the synthetic stub for *name*.

        Args:
            name: Credential variable name.

        Returns:
            :data:`STUB_SECRET`, always.
        """
        del name
        return STUB_SECRET


class _StopLaunch(Exception):
    """Abort a provider call once its payload has been captured."""


# ---------------------------------------------------------------------------
# Spec construction — mirrors what the orchestrator does before create_instance
# ---------------------------------------------------------------------------


def compute_configs() -> list[Path]:
    """Return every example config that carries a ``compute:`` block.

    Returns:
        Sorted config paths, excluding those listed in
        :data:`EXCLUDED_CONFIGS`.
    """
    from kinoforge.core.config import load_config

    return [
        p
        for p in sorted(CONFIG_DIR.glob("*.yaml"))
        if p.name not in EXCLUDED_CONFIGS and load_config(str(p)).compute is not None
    ]


def _catalog_offer(cfg: Config) -> Offer:
    """Return the frozen synthetic offer standing in for a catalog lookup.

    RunPod and SkyPilot enumerate offers over a network. Both are given a
    deterministic offer built from the config's own placement block, so the
    golden pins the spec -> payload mapping (which S1 changes) rather than
    catalog contents (which S4 owns).

    Args:
        cfg: The loaded config.

    Returns:
        A GPU offer named after the config's first ``placement.accelerators``
        entry, or the synthetic CPU offer SkyPilot's ``find_offers`` returns
        for ``min_vram_gb == 0`` — the shape that makes ``create_instance``
        request ``cpus``/``memory`` instead of an accelerator.
    """
    from kinoforge.core.interfaces import Offer

    reqs = cfg.placement()
    if reqs.min_vram_gb == 0:
        # Mirrors SkyPilotProvider.find_offers' CPU short-circuit exactly.
        return Offer(
            id="sky-cpu-auto",
            gpu_type="",
            vram_gb=0,
            cuda="0.0",
            cost_rate_usd_per_hr=0.05,
            mode="pod",
        )
    gpu = reqs.accelerators[0] if reqs.accelerators else "NVIDIA A100 80GB PCIe"
    return Offer(
        id=gpu,
        gpu_type=gpu,
        vram_gb=80,
        cuda="12.4",
        cost_rate_usd_per_hr=1.64,
        mode="pod",
    )


def build_spec(cfg: Config) -> InstanceSpec:
    """Build the InstanceSpec the orchestrator would build, deterministically.

    Mirrors ``_provision_instance_and_build_backend`` in
    :mod:`kinoforge.core.orchestrator`: resolve the engine from the registry,
    dump the config, lift the resolved ``Lifecycle`` onto ``cfg_dict``, render
    the provision payload, then hand it all to
    :func:`kinoforge.core.spec_builder.build_instance_spec`.

    Args:
        cfg: The loaded config (must have a ``compute`` block).

    Returns:
        The spec that would be passed to ``provider.create_instance``.

    Raises:
        ValueError: ``cfg`` has no ``compute`` block.
    """
    import kinoforge._adapters  # noqa: F401 — registers engines + providers
    from kinoforge.core import registry
    from kinoforge.core.spec_builder import build_instance_spec

    if cfg.compute is None:
        raise ValueError("build_spec requires a config with a compute block")

    engine = registry.get_engine(cfg.engine.kind)()
    lifecycle = cfg.lifecycle()
    cfg_dict: dict[str, Any] = cfg.model_dump()
    # The orchestrator lifts the resolved Lifecycle onto cfg_dict so engines
    # can read canonical _s-suffixed keys; render_provision depends on it.
    cfg_dict["lifecycle"] = dataclasses.asdict(lifecycle)
    rendered = engine.render_provision(cfg_dict)
    return build_instance_spec(
        cfg=cfg,
        rendered=rendered,
        offer=_catalog_offer(cfg),
        engine_name=engine.name,
        key_hash=GOLDEN_KEY_HASH,
        image=cfg.compute.image,
        lifecycle=lifecycle,
        env=dict.fromkeys(rendered.env_required, STUB_SECRET),
        run_id=GOLDEN_RUN_ID,
        tags=None,
        diagnostic_env=None,
    )


# ---------------------------------------------------------------------------
# Per-provider capture
# ---------------------------------------------------------------------------


def _capture_runpod(cfg: Config, spec: InstanceSpec) -> dict[str, Any]:
    """Capture RunPod's ``podFindAndDeployOnDemand`` mutation input.

    Seam: the injected ``http_post`` transport, the same one
    ``tests/providers/test_runpod_create_pod_cloud_type.py`` uses. The GraphQL
    query text itself is deliberately dropped — it is a static module constant,
    not a spec-derived value, and keeping it would bury the payload the ratchet
    cares about under a wall of unrelated SDL.

    Args:
        cfg: The loaded config (unused; kept for signature symmetry).
        spec: The spec to launch.

    Returns:
        ``{"provider": ..., "seam": ..., "input": <variables.input dict>}``.
    """
    del cfg
    from kinoforge.providers.runpod import RunPodProvider

    captured: list[dict[str, Any]] = []

    def _http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        del url
        captured.append(body)
        return {"data": {"podFindAndDeployOnDemand": {"id": "pod-golden"}}}

    provider = RunPodProvider(
        _StubCreds(),  # type: ignore[arg-type]
        http_post=_http_post,
        http_get=lambda _url: {},
    )
    provider.create_instance(spec)
    return {
        "provider": "runpod",
        "seam": "http_post -> podFindAndDeployOnDemand variables.input",
        "input": captured[0]["variables"]["input"],
    }


def _capture_modal(cfg: Config, spec: InstanceSpec) -> dict[str, Any]:
    """Capture the ``ModalAppRequest`` Modal would build an App from.

    Seam: the injected ``app_factory``, as in
    ``tests/providers/modal/test_provider.py``. Modal has no wire dict of its
    own — the request dataclass *is* the payload, and everything downstream
    (image layers, ``@app.function`` kwargs, the web server) is derived from
    it inside ``build_modal_app``.

    Args:
        cfg: The loaded config (unused; kept for signature symmetry).
        spec: The spec to launch.

    Returns:
        ``{"provider": ..., "seam": ..., "request": <ModalAppRequest asdict>}``.
    """
    del cfg
    from kinoforge.providers.modal import ModalProvider

    captured: list[Any] = []

    def _app_factory(req: Any, modal_mod: Any) -> tuple[Any, Any]:  # noqa: ANN401
        del modal_mod
        captured.append(req)
        return object(), object()

    provider = ModalProvider(
        app_factory=_app_factory,
        deployer=lambda _app, _fn: "https://kinoforge-golden--server.modal.run",
        # A non-None module short-circuits the lazy `import modal`, which is
        # only installed in the live-modal env.
        modal_module=object(),
        clock=lambda: FROZEN_EPOCH,
    )
    provider.create_instance(spec)
    return {
        "provider": "modal",
        "seam": "app_factory -> ModalAppRequest",
        "request": dataclasses.asdict(captured[0]),
    }


def _capture_skypilot(cfg: Config, spec: InstanceSpec) -> dict[str, Any]:
    """Capture SkyPilot's ``(task_config, launch_kwargs)`` pair.

    Seam: the injected ``sky_client``'s ``Task.from_yaml_config`` +
    ``launch``, mirroring ``_FakeSky`` in ``tests/providers/test_skypilot.py``.
    ``launch`` raises :class:`_StopLaunch` once it has recorded the call, so
    the capture never reaches the ssh-tunnel spawn that follows a real launch.

    ``clouds`` / ``retry_until_up`` are pinned from
    ``cfg.compute.backend_options.skypilot`` exactly as
    :func:`kinoforge._adapters.build_provider_for` does — without the cloud
    pin the ``resources.cloud`` / ``resources.any_of`` keys would never
    appear.

    Args:
        cfg: The loaded config (supplies the cloud pin).
        spec: The spec to launch.

    Returns:
        ``{"provider": ..., "seam": ..., "task_config": ..., "launch_kwargs": ...}``.

    Raises:
        RuntimeError: ``sky.launch`` was never reached.
    """
    from kinoforge.providers.skypilot import SkyPilotProvider

    captured: dict[str, Any] = {}

    class _TaskNamespace:
        """Stand-in for ``sky.Task`` exposing the ``from_yaml_config`` factory."""

        @staticmethod
        def from_yaml_config(config: dict[str, Any]) -> dict[str, Any]:
            """Record and return the task config verbatim."""
            captured["task_config"] = config
            return config

    class _CapturingSky:
        """Minimal ``sky`` module stand-in that records the launch call."""

        Task = _TaskNamespace

        @staticmethod
        def launch(task: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            """Record ``launch_kwargs`` then abort before any real work."""
            del task
            captured["launch_kwargs"] = kwargs
            raise _StopLaunch

    assert cfg.compute is not None  # noqa: S101 — caller guarantees this
    sky_opts = cfg.backend_options_for("skypilot")
    provider = SkyPilotProvider(
        _CapturingSky(),
        clouds=list(sky_opts.clouds) if sky_opts.clouds else None,
        retry_until_up=sky_opts.retry_until_up,
    )
    try:
        provider.create_instance(spec)
    except _StopLaunch:
        pass
    if "launch_kwargs" not in captured:
        raise RuntimeError("skypilot capture never reached sky.launch")
    return {
        "provider": "skypilot",
        "seam": "sky.Task.from_yaml_config + sky.launch(**kwargs)",
        "task_config": captured["task_config"],
        "launch_kwargs": captured["launch_kwargs"],
    }


def _capture_local(cfg: Config, spec: InstanceSpec) -> dict[str, Any]:
    """Capture what LocalProvider records for a spec.

    Seam: the returned :class:`~kinoforge.core.interfaces.Instance` — the
    local provider has no wire at all, so the Instance it fabricates is the
    closest analogue of a payload.

    Args:
        cfg: The loaded config (unused; kept for signature symmetry).
        spec: The spec to launch.

    Returns:
        ``{"provider": ..., "seam": ..., "instance": {...}}``.
    """
    del cfg
    from kinoforge.core.clock import FakeClock
    from kinoforge.providers.local import LocalProvider

    instance = LocalProvider(clock=FakeClock(start=FROZEN_EPOCH)).create_instance(spec)
    return {
        "provider": "local",
        "seam": "LocalProvider.create_instance -> Instance",
        "instance": {
            "image": spec.image,
            "ports": list(spec.ports),
            "env": dict(spec.env),
            "run_cmd": list(spec.run_cmd or []),
            "cost_rate_usd_per_hr": instance.cost_rate_usd_per_hr,
            "status": instance.status,
            "tags": dict(instance.tags),
        },
    }


_CAPTURERS = {
    "runpod": _capture_runpod,
    "modal": _capture_modal,
    "skypilot": _capture_skypilot,
    "local": _capture_local,
}


def _frozen_clock() -> Any:  # noqa: ANN401
    """Return a context manager freezing ``time.time`` at :data:`FROZEN_EPOCH`.

    Patched on the ``time`` module itself rather than on any one provider:
    SkyPilot reads ``time.time`` for the watchdog deadline, Modal for
    ``Instance.created_at``, and an engine's rendered script may stamp one too.

    Returns:
        The active :func:`unittest.mock.patch` context manager.
    """
    return mock.patch.object(time, "time", return_value=FROZEN_EPOCH)


def capture_payload(config_path: Path) -> dict[str, Any]:
    """Return the wire payload the configured provider would send for a config.

    Args:
        config_path: Path to an ``examples/configs/*.yaml`` file.

    Returns:
        A JSON-serialisable payload dict, always carrying ``provider`` and
        ``seam`` keys naming what was captured and where.

    Raises:
        ValueError: The config has no ``compute`` block, or names a provider
            with no capture seam.
    """
    from kinoforge.core.config import load_config

    cfg = load_config(str(config_path))
    if cfg.compute is None:
        raise ValueError(f"{config_path} has no compute block")
    capturer = _CAPTURERS.get(cfg.compute.provider)
    if capturer is None:
        raise ValueError(
            f"no capture seam for provider {cfg.compute.provider!r} in {config_path}"
        )
    with _frozen_clock():
        spec = build_spec(cfg)
        return capturer(cfg, spec)


def serialize(payload: dict[str, Any]) -> str:
    """Serialise a payload to the exact bytes a golden file holds.

    Args:
        payload: The captured payload.

    Returns:
        Pretty-printed, key-sorted JSON with a trailing newline.
    """
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def golden_path_for(config_path: Path) -> Path:
    """Return the golden file path for *config_path*.

    Args:
        config_path: Path to an example config.

    Returns:
        ``tests/providers/golden/launch_payloads/<stem>.json``.
    """
    return GOLDEN_DIR / f"{config_path.stem}.json"


def _iter_written(paths: list[Path]) -> Iterator[Path]:
    """Write one golden per config and yield the paths written.

    Args:
        paths: Config paths to capture.

    Yields:
        Each golden path written.
    """
    for cfg_path in paths:
        out = golden_path_for(cfg_path)
        out.write_text(serialize(capture_payload(cfg_path)))
        yield out


def main() -> int:
    """Write one golden per example config with a compute block.

    Returns:
        Process exit code (always 0 — a capture failure raises instead).
    """
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    paths = compute_configs()
    written = list(_iter_written(paths))
    print(f"wrote {len(written)} goldens to {GOLDEN_DIR}")
    for name, reason in sorted(EXCLUDED_CONFIGS.items()):
        print(f"  excluded {name}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
