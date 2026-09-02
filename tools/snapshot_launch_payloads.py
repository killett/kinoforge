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
* Selection IS captured now, and is deterministic because the catalog it reads
  is frozen here rather than fetched. compute-seam S4 moved SKU selection
  inside each provider (``find_offers`` left the ABC and ``InstanceSpec.offer``
  is gone), so the harness can no longer inject a chosen offer. Instead
  :data:`_FROZEN_RUNPOD_CATALOG` and :data:`_FROZEN_SKY_CATALOG` are returned by
  the injected transports, and Modal reads its own static
  ``MODAL_GPU_CATALOG``. The determinism contract is therefore unchanged in
  substance — a captured payload still depends only on the config — but it now
  runs through the real selection code instead of around it, which means the
  golden also pins WHICH SKU a config selects.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from kinoforge.core.config import Config
    from kinoforge.core.interfaces import (
        Instance,
        InstanceSpec,
        RenderedProvision,
    )

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


class _RecordingLedger:
    """Capture the ORCHESTRATOR's pre-launch provisional row (finding F12).

    SkyPilot builds no Instance the capture can see — :class:`_StopLaunch`
    aborts inside ``sky.launch``, before ``create_instance`` returns. The
    provisional row written just before the create is the only Instance the
    capture can observe, and it is where ``spec.tags`` lands for the parity
    guard.

    compute-seam S5 moved that writer from the provider to the orchestrator, so
    this fake is now driven by
    :func:`kinoforge.core.orchestrator._record_provisional_row` rather than
    installed on the provider. Nothing about it touches ``task_config`` or
    ``launch_kwargs``, so the goldens are untouched either way.

    Its signature deliberately mirrors
    :meth:`kinoforge.core.lifecycle.Ledger.record` in full, so the fake cannot
    silently diverge from the real class if the writer starts passing another
    lifecycle keyword.
    """

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.recorded: list[Instance] = []

    def record(
        self,
        instance: Instance,
        *,
        idle_timeout_s: int | None = None,
        max_age_s: int | None = None,
    ) -> None:
        """Record *instance*.

        Args:
            instance: The provisional row.
            idle_timeout_s: Ignored; mirrors ``Ledger.record``.
            max_age_s: Ignored; mirrors ``Ledger.record``.
        """
        del idle_timeout_s, max_age_s
        self.recorded.append(instance)

    def forget(self, instance_id: str) -> None:
        """Ignore the forget call.

        Args:
            instance_id: Ignored; the capture never reaches the success path.
        """
        del instance_id

    def forget_provisional(
        self, provisional_id: str, *, real_id: str | None = None
    ) -> bool:
        """Ignore the collapse call.

        Args:
            provisional_id: Ignored; the capture never reaches the success path.
            real_id: Ignored, for the same reason. Optional, mirroring
                ``Ledger.forget_provisional`` — the failure path omits it.

        Returns:
            Always False — nothing was removed.
        """
        del provisional_id, real_id
        return False


@dataclasses.dataclass(frozen=True)
class Launch:
    """One captured launch: the wire payload, its spec, and the Instance.

    :func:`capture_payload` returns only :attr:`payload` — the bytes the
    golden ratchet freezes. The parity guard
    (``tests/providers/test_field_consumption_parity.py``) needs the other
    two as well: the spec so a proof can mutate one field and re-capture,
    and the Instance because a couple of portable fields (``tags``) are
    honoured on the record rather than on the wire.

    Attributes:
        payload: Exactly what :func:`capture_payload` returns.
        spec: The InstanceSpec that produced it, after any mutation.
        instance: The Instance the provider fabricated, or ``None`` when the
            capture aborts before one exists.
    """

    payload: dict[str, Any]
    spec: InstanceSpec
    instance: Instance | None


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


#: Frozen accelerator catalog for the SkyPilot capture. compute-seam S4: the
#: provider selects from this instead of from an offer the harness injected,
#: so the captured payload still depends only on the config. Entries are
#: ordered cheapest-first among those clearing a given VRAM floor, which is
#: what ``_select_accelerator`` picks between.
_FROZEN_SKY_CATALOG: dict[str, dict[str, Any]] = {
    "T4": {"accelerator_name": "T4", "vram_gb": 16, "cuda": "12.8", "price": 0.35},
    "L4": {"accelerator_name": "L4", "vram_gb": 24, "cuda": "12.8", "price": 0.80},
    "A100": {"accelerator_name": "A100", "vram_gb": 80, "cuda": "12.8", "price": 2.10},
    "H100": {"accelerator_name": "H100", "vram_gb": 80, "cuda": "12.8", "price": 3.95},
}


def _diagnostic_env(cfg: Config) -> dict[str, str] | None:
    """Return the synthetic diagnostic overlay for a capture, or ``None``.

    Mirrors the KEY SHAPE ``orchestrator._build_diagnostic_env`` produces —
    same bucket/region defaults, same ``boot-logs/<run_id>`` prefix — so a
    diagnostic-mode capture is no longer invisible to this tool (the gap
    flagged in the compute-seam S1 Task 7 review: this function used to be
    skipped entirely, so ``build_spec`` passed ``diagnostic_env=None``
    unconditionally regardless of ``cfg.diagnostic_mode``).

    Deliberately does NOT call the real ``_build_diagnostic_env`` — that
    function reads ``os.environ`` and resolves AWS credentials via the boto3
    default chain, either of which would make a capture depend on the
    calling machine's real environment/credentials. That violates this
    tool's determinism contract (frozen clock, synthetic secrets only) and
    risks a real credential landing in a checked-in golden file. Every value
    that would otherwise come from ``os.environ`` or boto3 is instead the
    synthetic :data:`STUB_SECRET`, or the same hardcoded default the
    orchestrator falls back to when no override is set.

    Args:
        cfg: The loaded config.

    Returns:
        The overlay dict when ``cfg.diagnostic_mode`` is set, else ``None``
        — keeping every non-diagnostic config's derivation at exactly the
        ``None`` it was before, so none of the 31 existing goldens move.
    """
    if not cfg.diagnostic_mode:
        return None
    return {
        "KINOFORGE_DIAG_BUCKET": "<DIAG_BUCKET>",
        "KINOFORGE_DIAG_PREFIX": f"boot-logs/{GOLDEN_RUN_ID}",
        "AWS_DEFAULT_REGION": "us-west-2",
        "AWS_ACCESS_KEY_ID": STUB_SECRET,
        "AWS_SECRET_ACCESS_KEY": STUB_SECRET,
    }


def render_provision_for(cfg: Config) -> RenderedProvision:
    """Return the payload the config's engine renders, without building a spec.

    The engine-render half of :func:`build_spec`, lifted out so a test can
    assert on what an ENGINE emits (``setup_steps`` / ``launch`` / ``script``)
    without going through provider capture. ``build_spec`` calls this, so the
    two can never drift.

    Args:
        cfg: The loaded config (must have a ``compute`` block).

    Returns:
        The engine's rendered provision payload.

    Raises:
        ValueError: ``cfg`` has no ``compute`` block.
    """
    import kinoforge._adapters  # noqa: F401 — registers engines + providers
    from kinoforge.core import registry

    if cfg.compute is None:
        raise ValueError("render_provision_for requires a config with a compute block")

    engine = registry.get_engine(cfg.engine.kind)()
    cfg_dict: dict[str, Any] = cfg.model_dump()
    # The orchestrator lifts the resolved Lifecycle onto cfg_dict so engines
    # can read canonical _s-suffixed keys; render_provision depends on it.
    cfg_dict["lifecycle"] = dataclasses.asdict(cfg.lifecycle())
    return engine.render_provision(cfg_dict)


def render_for_config(config_path: Path) -> RenderedProvision:
    """Return the payload the engine renders for the config at *config_path*.

    Args:
        config_path: Path to a YAML config carrying a ``compute:`` block.

    Returns:
        The engine's rendered provision payload.
    """
    from kinoforge.core.config import load_config

    return render_provision_for(load_config(str(config_path)))


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
    rendered = render_provision_for(cfg)
    return build_instance_spec(
        cfg=cfg,
        rendered=rendered,
        engine_name=engine.name,
        key_hash=GOLDEN_KEY_HASH,
        image=cfg.compute.image,
        lifecycle=lifecycle,
        env=dict.fromkeys(rendered.env_required, STUB_SECRET),
        run_id=GOLDEN_RUN_ID,
        tags=None,
        diagnostic_env=_diagnostic_env(cfg),
    )


# ---------------------------------------------------------------------------
# Per-provider capture
# ---------------------------------------------------------------------------


#: Frozen RunPod catalog for the capture. Wide enough that every shipped
#: config's placement finds its named accelerator, priced under the caps those
#: configs set.
_FROZEN_RUNPOD_CATALOG: dict[str, Any] = {
    "data": {
        "gpuTypes": [
            {
                "id": name,
                "displayName": name,
                "memoryInGb": vram,
                "secureCloud": True,
                "lowestPrice": {
                    "minimumBidPrice": price,
                    "uninterruptablePrice": price,
                },
            }
            # Every accelerator any shipped runpod config names, priced under
            # the tightest max_usd_per_hr among the configs that name it, so
            # the pre-book price filter never empties a catalog the capture is
            # not about. Derived from the configs, not invented.
            for name, vram, price in (
                ("NVIDIA RTX A4000", 16, 0.32),
                ("NVIDIA RTX A5000", 24, 0.34),
                ("NVIDIA GeForce RTX 4090", 24, 0.34),
                ("NVIDIA RTX 4090", 24, 0.34),
                ("RTX 4090", 24, 0.69),
                ("NVIDIA GeForce RTX 3090", 24, 0.34),
                ("NVIDIA L4", 24, 0.43),
                ("A100 40GB", 40, 1.19),
                # PCIe before the bare "A100 80GB" alias, deliberately: a
                # config that names NO accelerator takes the first survivor in
                # catalog order, and this ordering keeps those two goldens
                # (cost, sweeper) pinned to the same SKU S1-S3 measured. The
                # order of a real RunPod catalog response is not something
                # kinoforge controls, so pinning one here is a fixture choice,
                # not a claim about the API.
                ("NVIDIA A100 80GB PCIe", 80, 1.64),
                ("A100 80GB", 80, 1.64),
                ("NVIDIA A100-SXM4-80GB", 80, 1.89),
                ("H100 80GB", 80, 2.39),
                ("NVIDIA H100 80GB HBM3", 80, 2.39),
                ("NVIDIA H100 PCIe", 80, 2.39),
                ("NVIDIA H100 NVL", 80, 2.49),
            )
        ]
    }
}


def _capture_runpod(
    cfg: Config, spec: InstanceSpec
) -> tuple[dict[str, Any], Instance | None]:
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
        ``({"provider": ..., "seam": ..., "input": <variables.input dict>},
        instance)``.
    """
    del cfg
    from kinoforge.providers.runpod import RunPodProvider

    captured: list[dict[str, Any]] = []

    def _http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        del url
        captured.append(body)
        if "gpuTypes" in str(body.get("query", "")):
            # compute-seam S4: RunPod selects from its own catalog inside
            # create_instance. Frozen here so the captured payload still
            # depends only on the config, never on live catalog contents.
            return _FROZEN_RUNPOD_CATALOG
        return {"data": {"podFindAndDeployOnDemand": {"id": "pod-golden"}}}

    provider = RunPodProvider(
        _StubCreds(),  # type: ignore[arg-type]
        http_post=_http_post,
        http_get=lambda _url: {},
    )
    instance = provider.create_instance(spec)
    return (
        {
            "provider": "runpod",
            "seam": "http_post -> podFindAndDeployOnDemand variables.input",
            # The create mutation, not the catalog read that now precedes it
            # (compute-seam S4): the ratchet is about the payload, and index 0
            # stopped being the payload the moment selection moved inside the
            # provider.
            "input": next(
                b["variables"]["input"] for b in captured if "variables" in b
            ),
        },
        instance,
    )


def _capture_modal(
    cfg: Config, spec: InstanceSpec
) -> tuple[dict[str, Any], Instance | None]:
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
        ``({"provider": ..., "seam": ..., "request": <ModalAppRequest asdict>},
        instance)``.
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
    instance = provider.create_instance(spec)
    return (
        {
            "provider": "modal",
            "seam": "app_factory -> ModalAppRequest",
            "request": dataclasses.asdict(captured[0]),
        },
        instance,
    )


def _capture_skypilot(
    cfg: Config, spec: InstanceSpec
) -> tuple[dict[str, Any], Instance | None]:
    """Capture SkyPilot's ``(task_config, launch_kwargs)`` pair.

    Seam: the injected ``sky_client``'s ``Task.from_yaml_config`` +
    ``launch``, mirroring ``_FakeSky`` in ``tests/providers/test_skypilot.py``.
    ``launch`` raises :class:`_StopLaunch` once it has recorded the call, so
    the capture never reaches the ssh-tunnel spawn that follows a real launch.

    ``clouds`` / ``retry_until_up`` are pinned from
    ``cfg.compute.backend_options.skypilot``, and ``region`` from
    ``cfg.placement()``, exactly as
    :func:`kinoforge._adapters.build_provider_for` does — without the cloud
    pin the ``resources.cloud`` / ``resources.any_of`` keys would never
    appear, and without the region pin ``resources.region`` never would
    either, so a capture would show an unpinned launch the CLI does not make.

    Args:
        cfg: The loaded config (supplies the cloud pin).
        spec: The spec to launch.

    Returns:
        ``({"provider": ..., "seam": ..., "task_config": ...,
        "launch_kwargs": ...}, provisional_instance)``.

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
        """Minimal ``sky`` module stand-in that records the launch call.

        Also answers ``list_accelerators`` from a FROZEN catalog: compute-seam
        S4 made selection the provider's business, so a capture that could not
        answer it would either crash or depend on a live catalog — and the
        whole point of this ratchet is that a payload depends only on the
        config.
        """

        Task = _TaskNamespace

        @staticmethod
        def list_accelerators(**kwargs: Any) -> dict[str, list[dict[str, Any]]]:  # noqa: ANN401
            """Return the frozen offline catalog (never a network call)."""
            del kwargs
            return {name: [dict(rec)] for name, rec in _FROZEN_SKY_CATALOG.items()}

        class Dag:
            """Stand-in for ``sky.Dag`` so the estimate reaches ``optimize``.

            Without it the provider's ``sky.Dag()`` would AttributeError and
            the estimate would come back unreadable for the wrong reason —
            the capture would still be correct, but it would stop exercising
            the ``optimize`` refusal below.
            """

            def __init__(self) -> None:
                self.tasks: list[Any] = []

            def add(self, task: Any) -> None:  # noqa: ANN401
                """Append ``task``, mirroring ``sky.Dag.add``."""
                self.tasks.append(task)

        @staticmethod
        def optimize(dag: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            """Refuse to estimate — the capture must not depend on a server.

            Real ``sky.optimize`` POSTs to ``/optimize``. Raising makes the
            provider's estimate unreadable, which is the documented
            WARN-and-proceed path, so the captured payload is exactly the one
            a real launch sends and the goldens stay byte-identical.
            """
            del dag, kwargs
            raise RuntimeError("offline capture: no optimizer")

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
        region=cfg.placement().region,
        retry_until_up=sky_opts.retry_until_up,
    )
    ledger = _RecordingLedger()
    from kinoforge.core.orchestrator import _record_provisional_row

    # compute-seam S5: the provisional row moved to the orchestrator, so the
    # capture writes it the same way deploy_session does. It is still the only
    # Instance a skypilot capture can observe — _StopLaunch aborts inside
    # sky.launch — and it is still where spec.tags lands for the parity guard.
    _record_provisional_row(
        ledger=ledger,
        run_id=spec.run_id,
        provider_name="skypilot",
        tags=dict(spec.tags),
        max_age_s=int(spec.lifecycle.max_lifetime_s),
        now=FROZEN_EPOCH,
    )
    try:
        provider.create_instance(spec)
    except _StopLaunch:
        pass
    if "launch_kwargs" not in captured:
        raise RuntimeError("skypilot capture never reached sky.launch")
    return (
        {
            "provider": "skypilot",
            "seam": "sky.Task.from_yaml_config + sky.launch(**kwargs)",
            "task_config": captured["task_config"],
            "launch_kwargs": captured["launch_kwargs"],
        },
        ledger.recorded[0] if ledger.recorded else None,
    )


def _capture_local(
    cfg: Config, spec: InstanceSpec
) -> tuple[dict[str, Any], Instance | None]:
    """Capture what LocalProvider records for a spec.

    Seam: the returned :class:`~kinoforge.core.interfaces.Instance` — the
    local provider has no wire at all, so the Instance it fabricates is the
    closest analogue of a payload.

    Args:
        cfg: The loaded config (unused; kept for signature symmetry).
        spec: The spec to launch.

    Returns:
        ``({"provider": ..., "seam": ..., "instance": {...}}, instance)``.
    """
    del cfg
    from kinoforge.core.clock import FakeClock
    from kinoforge.providers.local import LocalProvider

    instance = LocalProvider(clock=FakeClock(start=FROZEN_EPOCH)).create_instance(spec)
    return (
        {
            "provider": "local",
            "seam": "LocalProvider.create_instance -> Instance",
            "instance": {
                # NOTE: image / ports / env are echoed from the SPEC, not
                # read off the Instance — LocalProvider ignores all three.
                # Anything proving a local declaration must observe the
                # Instance (below), never these keys.
                "image": spec.image,
                "ports": list(spec.ports),
                "env": dict(spec.env),
                "cost_rate_usd_per_hr": instance.cost_rate_usd_per_hr,
                "status": instance.status,
                "tags": dict(instance.tags),
            },
        },
        instance,
    )


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


def capture_launch(
    config_path: Path,
    *,
    mutate_cfg: Callable[[Config], Config] | None = None,
    mutate_spec: Callable[[InstanceSpec], InstanceSpec] | None = None,
) -> Launch:
    """Capture one launch, optionally with the config or spec perturbed.

    The mutation hooks exist for the field-consumption parity guard: proving
    that a provider READS a portable field means changing that field and
    watching the payload follow. Without them a proof can only assert that
    some key is present, which a hardcoded value satisfies just as well.

    Args:
        config_path: Path to an ``examples/configs/*.yaml`` file.
        mutate_cfg: Applied to the loaded config before the spec is built —
            the route for ``compute.backend_options``, whose SkyPilot
            namespace is consumed by the composition root rather than read
            off the spec.
        mutate_spec: Applied to the built spec before ``create_instance``.

    Returns:
        The :class:`Launch` for this config.

    Raises:
        ValueError: The config has no ``compute`` block, or names a provider
            with no capture seam.
    """
    from kinoforge.core.config import load_config

    cfg = load_config(str(config_path))
    if mutate_cfg is not None:
        cfg = mutate_cfg(cfg)
    compute = cfg.compute
    if compute is None:
        raise ValueError(f"{config_path} has no compute block")
    capturer = _CAPTURERS.get(compute.provider)
    if capturer is None:
        raise ValueError(
            f"no capture seam for provider {compute.provider!r} in {config_path}"
        )
    with _frozen_clock():
        spec = build_spec(cfg)
        if mutate_spec is not None:
            spec = mutate_spec(spec)
        payload, instance = capturer(cfg, spec)
    return Launch(payload=payload, spec=spec, instance=instance)


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
    return capture_launch(config_path).payload


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
