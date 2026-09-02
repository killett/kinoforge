"""Behavior: every portable field is declared by every provider, and CONSUMED is proven.

This is the regression guard the whole compute-seam rework exists to install.
F5 counted 10 of 17 InstanceSpec fields ignored by at least one provider, with
nothing anywhere that noticed. After this test, adding a field without deciding
what each provider does with it breaks the suite, and claiming to consume a
field you drop on the floor breaks it too.

Two proof surfaces, because a portable field reaches a provider by two routes
and only declaring both is honest:

* **launch** — the wire payload ``tools/snapshot_launch_payloads`` captures.
  A proof mutates one spec (or config) field, re-captures, and requires the
  observed payload value to FOLLOW the mutation. Asserting a key merely
  exists would pass for a provider that hardcodes it — which is exactly the
  bug ``accelerator_count`` and ``disk_gb`` turned out to be.
* **catalog** — the provider's own ``find_offers``. ``Placement``'s
  selection fields (``min_vram_gb`` / ``min_cuda`` / ``max_usd_per_hr`` /
  ``accelerators``) never appear in a launch payload: they are applied while
  enumerating offers, and the chosen ``Offer`` is what the payload carries.
  A proof narrows one axis until the whole catalog is excluded.
"""

from __future__ import annotations

import base64
import dataclasses
import functools
import gzip
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

import kinoforge._adapters  # noqa: F401 — composition root; the providers self-register
from kinoforge.core import registry
from kinoforge.core.interfaces import (
    CredentialProvider,
    FieldSupport,
    InstanceSpec,
    Offer,
    Placement,
)

# Aliased: ``Launch`` is already the name of the CAPTURED-launch record this
# module imports from the snapshot tool. These two are the spec fields.
from kinoforge.core.interfaces import Launch as SpecLaunch
from kinoforge.core.interfaces import SetupStep as SpecSetupStep
from kinoforge.providers.local import LocalProvider
from kinoforge.providers.modal import ModalProvider
from kinoforge.providers.runpod import RunPodProvider
from kinoforge.providers.skypilot import SkyPilotProvider
from tools.snapshot_launch_payloads import Launch, capture_launch

_SPEC_PORTABLE = {
    "image",
    "ports",
    "volume_gb",
    "volume_mount",
    "env",
    "tags",
    "run_id",
    "setup_steps",
    "launch",
    "lifecycle",
    "backend_options",
}

#: Every provider the composition root registers. Named explicitly so the
#: parametrized tests below cannot pass vacuously: an import-order regression
#: that leaves the registry empty turns every ``parametrize`` into zero cases,
#: which reads as a green suite. This set is the tripwire for that.
_EXPECTED_PROVIDERS = frozenset({"local", "modal", "runpod", "skypilot"})

_CONFIG_DIR = Path("examples/configs")

#: One representative shipped config per provider. Each is already frozen as a
#: golden launch payload, so a proof here and the ratchet observe the same wire.
_REPRESENTATIVE_CONFIG: dict[str, Path] = {
    "local": _CONFIG_DIR / "local-fake.yaml",
    "modal": _CONFIG_DIR / "modal-diffusers-wan-2_1-1_3b-t2v.yaml",
    "runpod": _CONFIG_DIR / "runpod-diffusers-rife-60fps-interpolate.yaml",
    "skypilot": _CONFIG_DIR / "skypilot-lambda-comfyui.yaml",
}


#: ``ComputeConfig`` fields that are STRUCTURE rather than a launch decision,
#: and so need no per-provider declaration:
#:
#: * ``provider`` selects which declaration table applies at all.
#: * ``placement`` is declared through its own sub-fields (``accelerators``,
#:   ``region``, ...), not as one opaque blob.
#: * ``lifecycle`` is covered by the ``lifecycle`` row already required of
#:   every provider via ``_SPEC_PORTABLE``.
#: * ``backend_options`` is likewise already a required row, and is by
#:   definition provider-specific.
#:
#: Everything else in the block describes WHAT TO LAUNCH and must be declared.
#: ``test_the_structural_exclusion_set_is_not_stale`` pins this against
#: ``ComputeConfig.model_fields`` in both directions, so a renamed field
#: cannot leave a ghost here that quietly stops requiring its replacement.
_COMPUTE_STRUCTURAL = {"provider", "placement", "lifecycle", "backend_options"}


def _compute_portable() -> set[str]:
    """Return the compute-level fields every provider must declare.

    Returns:
        ``ComputeConfig``'s fields minus :data:`_COMPUTE_STRUCTURAL`.
    """
    from kinoforge.core.config import ComputeConfig  # noqa: PLC0415

    return set(ComputeConfig.model_fields) - _COMPUTE_STRUCTURAL


def _expected_fields() -> set[str]:
    """Return the portable field set every provider must declare.

    Returns:
        The union of ``Placement``'s fields, the portable subset of
        ``InstanceSpec``'s, and the compute-level fields.
    """
    placement = {f.name for f in dataclasses.fields(Placement)}
    spec = {f.name for f in dataclasses.fields(InstanceSpec)} & _SPEC_PORTABLE
    return placement | spec | _compute_portable()


# ---------------------------------------------------------------------------
# Declaration parity
# ---------------------------------------------------------------------------


def _shipped_providers() -> set[str]:
    """Return the registered providers that ship inside ``kinoforge.providers``.

    The registry is process-global and other tests register fakes into it,
    so a bare ``provider_names()`` comparison is not stable. Filtering by
    defining module keeps the real four and drops the fakes.

    Returns:
        Names of the registered production providers.
    """
    shipped = set()
    for name in registry.provider_names():
        cls = registry.provider_class(name)
        if cls is not None and cls.__module__.startswith("kinoforge.providers"):
            shipped.add(name)
    return shipped


def test_every_shipped_provider_is_guarded_here() -> None:
    """The parametrized tests below cover every provider that ships.

    Bug caught, two ways. A fifth provider is added to the composition root
    and nobody declares its field consumption — the parametrize lists below
    are literals, so without this it would simply never be tested. And in
    the other direction, an import-order regression that leaves the registry
    empty turns a ``parametrize(provider_names())`` style guard into zero
    cases, which reads as green; this is the tripwire for that too.
    """
    assert _shipped_providers() == set(_EXPECTED_PROVIDERS)
    assert set(_REPRESENTATIVE_CONFIG) == set(_EXPECTED_PROVIDERS)
    assert set(_WIRE_PROOFS) <= set(_EXPECTED_PROVIDERS)


@pytest.mark.parametrize("provider_name", sorted(_EXPECTED_PROVIDERS))
def test_declaration_covers_exactly_the_portable_field_set(provider_name: str) -> None:
    """A provider declares every portable field, and only fields that exist.

    Bug caught (both directions): a field is added to ``Placement`` and some
    provider silently ignores it (F5's whole finding), or a field is deleted
    and a provider keeps declaring the ghost.
    """
    cls = registry.provider_class(provider_name)
    assert cls is not None
    declared = dict(cls.consumes())
    expected = _expected_fields()
    missing = expected - set(declared)
    stale = set(declared) - expected
    assert not missing, (
        f"{provider_name} does not declare {sorted(missing)}; a field nobody "
        "declares is a field that can be silently ignored"
    )
    assert not stale, f"{provider_name} declares removed fields {sorted(stale)}"
    assert all(isinstance(v, FieldSupport) for v in declared.values())


def test_compute_level_fields_are_declared_by_every_provider() -> None:
    """Every launch-describing key of the compute block has a declaration.

    Bug caught: ``consumes()`` covered ``Placement`` and part of
    ``InstanceSpec``, so ``compute.mode`` rotted for months — written by 46
    configs, read by nobody, and invisible to this guard because the guard
    never looked at the compute block at all.
    """
    expected = _compute_portable()
    assert expected, (
        "ComputeConfig lost every declarable field — check the exclusion set"
    )
    for name in sorted(_EXPECTED_PROVIDERS):
        cls = registry.provider_class(name)
        assert cls is not None
        missing = expected - set(cls.consumes())
        assert not missing, f"{name} does not declare {sorted(missing)}"


def test_the_structural_exclusion_set_is_not_stale() -> None:
    """``_COMPUTE_STRUCTURAL`` names only fields that still exist.

    Bug caught: a field is renamed, the exclusion set keeps the old name, and
    the subtraction in :func:`_compute_portable` silently stops requiring the
    new one — the same class of rot the guard exists to prevent, one level up.
    """
    from kinoforge.core.config import ComputeConfig  # noqa: PLC0415

    fields = set(ComputeConfig.model_fields)
    stale = _COMPUTE_STRUCTURAL - fields
    assert not stale, f"_COMPUTE_STRUCTURAL names removed fields {sorted(stale)}"


def test_spec_portable_set_is_not_stale() -> None:
    """``_SPEC_PORTABLE`` and ``InstanceSpec`` must agree in BOTH directions.

    Bug caught (forward): a field leaves ``InstanceSpec`` and ``_SPEC_PORTABLE``
    keeps naming it. The intersection in :func:`_expected_fields` would then
    quietly stop requiring it of anyone.

    Bug caught (reverse): a later stage adds a field to ``InstanceSpec`` and
    declares it nowhere. Without this direction the new field needs no
    ``consumes()`` entry from any provider and nothing fails — a field every
    provider can silently ignore, which is the exact hole this guard exists to
    close. Adding the field here is the deliberate act that forces the
    per-provider declarations in
    :func:`test_declaration_covers_exactly_the_portable_field_set`.

    ``placement`` is the one exclusion, and only because its sub-fields are
    declared individually (``accelerators``, ``min_vram_gb``, ...) rather than
    as one opaque blob.
    """
    spec_fields = {f.name for f in dataclasses.fields(InstanceSpec)}
    assert _SPEC_PORTABLE <= spec_fields, sorted(_SPEC_PORTABLE - spec_fields)
    undeclared = spec_fields - {"placement"} - _SPEC_PORTABLE
    assert spec_fields - {"placement"} <= _SPEC_PORTABLE, (
        f"InstanceSpec fields {sorted(undeclared)} are named in neither "
        "_SPEC_PORTABLE nor the placement exclusion; add them to _SPEC_PORTABLE "
        "so every provider must declare how it treats them"
    )


def test_the_default_declaration_claims_nothing() -> None:
    """An undeclared provider must claim nothing, matching ``capabilities()``.

    Bug caught: the default is changed to something permissive, so a new
    provider that forgot to declare inherits a set of claims it never made.
    """
    from kinoforge.core.interfaces import ComputeProvider  # noqa: PLC0415

    assert dict(ComputeProvider.consumes()) == {}


# ---------------------------------------------------------------------------
# Proof substrate: launch captures
# ---------------------------------------------------------------------------

_Mutator = Callable[[InstanceSpec], InstanceSpec]
#: A proof answers "" when the field is proven, else the reason it is not.
_Proof = Callable[[str], str]
_Observe = Callable[[Launch], Any]


def _replaced(overrides: Mapping[str, Any]) -> _Mutator:
    """Return a spec mutator applying ``overrides``.

    Args:
        overrides: Field name to replacement value. A dotted name replaces a
            field of a nested dataclass (``"placement.spot"``,
            ``"lifecycle.idle_timeout_s"``, ``"offer.gpu_type"``).

    Returns:
        A callable suitable for ``capture_launch(mutate_spec=...)``.
    """

    def mutate(spec: InstanceSpec) -> InstanceSpec:
        flat: dict[str, Any] = {}
        nested: dict[str, dict[str, Any]] = {}
        for key, value in overrides.items():
            head, _, tail = key.partition(".")
            if tail:
                nested.setdefault(head, {})[tail] = value
            else:
                flat[head] = value
        for head, inner in nested.items():
            flat[head] = dataclasses.replace(getattr(spec, head), **inner)
        return dataclasses.replace(spec, **flat)

    return mutate


def _with_backend_options(
    namespaces: Mapping[str, Mapping[str, Any]],
) -> Callable[[Any], Any]:
    """Return a config mutator replacing ``compute.backend_options``.

    ``backend_options`` is the one portable field whose consumer is not
    always the provider object: SkyPilot's namespace is read by
    ``kinoforge._adapters.build_provider_for`` and arrives as constructor
    arguments. Probing at config level exercises the route an operator
    actually uses, for every provider alike.

    Args:
        namespaces: The replacement ``backend_options`` mapping.

    Returns:
        A callable suitable for ``capture_launch(mutate_cfg=...)``.
    """

    def mutate(cfg: Any) -> Any:  # noqa: ANN401 — Config, imported lazily by the tool
        clone = cfg.model_copy(deep=True)
        assert clone.compute is not None  # noqa: S101 — every representative config has one
        clone.compute.backend_options = {k: dict(v) for k, v in namespaces.items()}
        return clone

    return mutate


def _with_placement(overrides: Mapping[str, Any]) -> Callable[[Any], Any]:
    """Return a config mutator applying ``overrides`` to ``compute.placement``.

    ``region`` is the second portable field (after ``backend_options``) whose
    consumer is the composition root rather than the provider object: the
    registry factory takes no arguments, so
    :func:`kinoforge._adapters.build_provider_for` pins it onto the provider
    after construction. A spec-level probe could never observe that, so the
    probe is at config level — the route an operator actually uses.

    Args:
        overrides: Placement field name to replacement value.

    Returns:
        A callable suitable for ``capture_launch(mutate_cfg=...)``.
    """

    def mutate(cfg: Any) -> Any:  # noqa: ANN401 — Config, imported lazily by the tool
        clone = cfg.model_copy(deep=True)
        assert clone.compute is not None  # noqa: S101 — every representative config has one
        for key, value in overrides.items():
            setattr(clone.compute.placement, key, value)
        return clone

    return mutate


@functools.cache
def _baseline(provider_name: str) -> Launch:
    """Return the unmutated launch for ``provider_name``'s representative config."""
    return capture_launch(_REPRESENTATIVE_CONFIG[provider_name])


def _probed(
    provider_name: str,
    *,
    spec: _Mutator | None = None,
    cfg: Callable[[Any], Any] | None = None,
) -> Launch:
    """Return the launch captured with ``spec`` / ``cfg`` mutations applied."""
    return capture_launch(
        _REPRESENTATIVE_CONFIG[provider_name], mutate_spec=spec, mutate_cfg=cfg
    )


def _tracks(observe: _Observe, *, probe: Mapping[str, Any], expected: Any) -> _Proof:  # noqa: ANN401
    """Proof: the observed launch value FOLLOWS a mutation of the spec field.

    Args:
        observe: Reads the value under test out of a captured launch.
        probe: Spec overrides (see :func:`_replaced`) the mutation applies.
        expected: What ``observe`` must return once the probe is applied.

    Returns:
        A proof that fails when the observed value does not move — i.e. when
        the provider hardcodes it — and also when the baseline already equals
        ``expected``, which would make the probe unable to tell the two apart.
    """

    def proof(provider_name: str) -> str:
        before = observe(_baseline(provider_name))
        if before == expected:
            return (
                f"the unmutated payload already observes {expected!r}; this "
                "probe cannot tell a read from a constant"
            )
        after = observe(_probed(provider_name, spec=_replaced(probe)))
        if after != expected:
            return (
                f"spec probe {dict(probe)!r} left the payload at {after!r} "
                f"(wanted {expected!r}); the provider is not reading the field"
            )
        return ""

    return proof


def _tracks_cfg(
    observe: _Observe,
    *,
    backend_options: Mapping[str, Mapping[str, Any]],
    expected: Any,  # noqa: ANN401
) -> _Proof:
    """Proof: the observed launch value follows ``compute.backend_options``."""

    def proof(provider_name: str) -> str:
        before = observe(_baseline(provider_name))
        if before == expected:
            return (
                f"the unmutated payload already observes {expected!r}; this "
                "probe cannot tell a read from a constant"
            )
        after = observe(
            _probed(provider_name, cfg=_with_backend_options(backend_options))
        )
        if after != expected:
            return (
                f"backend_options probe {dict(backend_options)!r} left the "
                f"payload at {after!r} (wanted {expected!r})"
            )
        return ""

    return proof


def _runpod_mode_selects_its_mutation() -> _Proof:
    """Proof: ``spec.tags["mode"]`` decides which RunPod mutation is sent.

    ``compute.mode`` cannot be proven through
    :func:`~tools.snapshot_launch_payloads.capture_launch` the way the other
    fields are: that capturer hardcodes the pod mutation's response shape, so
    a serverless capture would be observing the harness rather than the
    provider. This drives the real provider twice over one transport instead
    and compares the two queries — the exact thing that did NOT differ before
    S2, when 46 configs wrote ``mode`` and nothing read it.

    Returns:
        A proof that fails when both modes put the same mutation on the wire.
    """

    def proof(provider_name: str) -> str:
        spec = _baseline(provider_name).spec

        def sent_query(mode: str) -> str:
            sent: list[dict[str, Any]] = []

            def post(_url: str, body: dict[str, Any]) -> dict[str, Any]:
                sent.append(body)
                if "gpuTypes" in str(body.get("query", "")):
                    # S4: create_instance reads the catalog before it creates.
                    return _RUNPOD_GPU_TYPES
                return {
                    "data": {
                        "podFindAndDeployOnDemand": {"id": "pod-probe"},
                        "saveTemplate": {"id": "sl-probe"},
                    }
                }

            provider = RunPodProvider(
                _StubCreds(), http_post=post, http_get=lambda _url: {}
            )
            provider.create_instance(
                dataclasses.replace(spec, tags={**spec.tags, "mode": mode})
            )
            return str(
                next(
                    b["query"]
                    for b in sent
                    if "gpuTypes" not in str(b.get("query", ""))
                )
            )

        pod, serverless = sent_query("pod"), sent_query("serverless")
        if pod == serverless:
            return (
                "mode=pod and mode=serverless put the SAME mutation on the "
                "wire; the branch is not reading the tag"
            )
        if "podFindAndDeployOnDemand" not in pod:
            return f"mode=pod did not send the pod mutation; sent {pod[:60]!r}"
        if "saveTemplate" not in serverless:
            return (
                "mode=serverless did not send the endpoint mutation; sent "
                f"{serverless[:60]!r}"
            )
        return ""

    return proof


def _runpod_heartbeat_mode_selects_its_substrate() -> _Proof:
    """Proof: ``compute.heartbeat_mode`` decides which substrate is built.

    Like ``backend_options``, this field's consumer is the composition root
    rather than the provider object — ``build_heartbeat_endpoint_for`` maps
    the value onto a concrete :class:`HeartbeatEndpoint`. So the proof
    observes what that returns for each value, which is where the operator's
    setting either takes effect or does not.

    Returns:
        A proof that fails when both values build the same thing.
    """

    def proof(provider_name: str) -> str:
        from kinoforge._adapters import build_heartbeat_endpoint_for  # noqa: PLC0415
        from kinoforge.providers.runpod.heartbeat import (  # noqa: PLC0415
            RunPodGraphQLHeartbeatEndpoint,
        )

        class _KeyedCreds(CredentialProvider):
            """Answers the one key the graphql-tag branch demands."""

            def get(self, key: str) -> str | None:
                """Return a synthetic RunPod key, nothing else."""
                return "kinoforge-prod-deadbeef" if key == "RUNPOD_API_KEY" else None

        from kinoforge.core.config import load_config  # noqa: PLC0415

        def built(mode: str) -> Any:  # noqa: ANN401 — HeartbeatEndpoint | None
            cfg = load_config(str(_REPRESENTATIVE_CONFIG[provider_name]))
            assert cfg.compute is not None  # noqa: S101 — representative configs have one
            cfg.compute.heartbeat_mode = mode
            return build_heartbeat_endpoint_for(cfg, _KeyedCreds())

        if built("none") is not None:
            return "heartbeat_mode='none' still built a substrate"
        if not isinstance(built("graphql-tag"), RunPodGraphQLHeartbeatEndpoint):
            return (
                "heartbeat_mode='graphql-tag' did not build the GraphQL "
                "substrate; the value is not being read"
            )
        return ""

    return proof


def _tracks_cfg_placement(
    observe: _Observe,
    *,
    placement: Mapping[str, Any],
    expected: Any,  # noqa: ANN401
) -> _Proof:
    """Proof: the observed launch value follows ``compute.placement``."""

    def proof(provider_name: str) -> str:
        before = observe(_baseline(provider_name))
        if before == expected:
            return (
                f"the unmutated payload already observes {expected!r}; this "
                "probe cannot tell a read from a constant"
            )
        after = observe(_probed(provider_name, cfg=_with_placement(placement)))
        if after != expected:
            return (
                f"placement probe {dict(placement)!r} left the payload at "
                f"{after!r} (wanted {expected!r})"
            )
        return ""

    return proof


# ---------------------------------------------------------------------------
# Proof substrate: offer catalogs
# ---------------------------------------------------------------------------


class _StubCreds(CredentialProvider):
    """Credential provider that answers nothing; ``find_offers`` needs none."""

    def get(self, key: str) -> str | None:
        """Return ``None`` for every lookup."""
        del key
        return None


#: Two RunPod GPU types with distinct names, VRAM and price, so each catalog
#: probe below has something to exclude and something to re-order.
_RUNPOD_GPU_TYPES: dict[str, Any] = {
    "data": {
        "gpuTypes": [
            {
                "id": "KF-PROBE-BIG",
                "memoryInGb": 80,
                "lowestPrice": {"uninterruptablePrice": 2.0, "minimumBidPrice": 1.0},
            },
            {
                "id": "KF-PROBE-SMALL",
                "memoryInGb": 24,
                "lowestPrice": {"uninterruptablePrice": 0.5, "minimumBidPrice": 0.25},
            },
        ]
    }
}

#: ``sky.list_accelerators``' offline-fake shape (flat list of dict records).
_SKY_ACCELERATORS: list[dict[str, Any]] = [
    {"name": "KF-PROBE-BIG", "vram_gb": 80, "cuda": "12.8", "price": 2.0},
    {"name": "KF-PROBE-SMALL", "vram_gb": 24, "cuda": "12.8", "price": 0.5},
]


class _FakeSkyCatalog:
    """Minimal ``sky`` stand-in exposing only the catalog call."""

    @staticmethod
    def list_accelerators(**kwargs: Any) -> list[dict[str, Any]]:
        """Return the fixed offline accelerator records."""
        del kwargs
        return list(_SKY_ACCELERATORS)


def _runpod_offers(reqs: Placement) -> list[Offer]:
    """Return RunPod's own ``find_offers`` output over the fake GPU-type list."""
    provider = RunPodProvider(
        _StubCreds(),
        http_post=lambda _url, _body: _RUNPOD_GPU_TYPES,
        http_get=lambda _url: {},
    )
    return provider.find_offers(reqs)


def _skypilot_offers(reqs: Placement) -> list[Offer]:
    """Return the accelerators SkyPilot would consider, filtered and ranked.

    compute-seam S4 made SkyPilot's selection private (it has no bookable
    catalog to publish). The probe goes through ``_candidate_accelerators``,
    which is the filtering-and-ranking half of that selection — the half these
    placement proofs are about.
    """
    return SkyPilotProvider(_FakeSkyCatalog())._candidate_accelerators(reqs)  # noqa: SLF001


_CATALOGS: dict[str, Callable[[Placement], list[Offer]]] = {
    "local": lambda reqs: LocalProvider().find_offers(reqs),
    "modal": lambda reqs: ModalProvider().find_offers(reqs),
    "runpod": _runpod_offers,
    "skypilot": _skypilot_offers,
}

#: Deliberately permissive so every provider's catalog survives it and each
#: probe below narrows exactly one axis. ``min_vram_gb`` is 1 rather than 0
#: because 0 makes SkyPilot short-circuit to its synthetic CPU offer.
_BASE_REQS = Placement(
    min_vram_gb=1,
    min_cuda="0.0",
    max_usd_per_hr=1_000_000.0,
    accelerators=(),
    disk_gb=0,
)

_Narrow = Callable[[list[Offer]], Placement]


def _above_every_vram(base: list[Offer]) -> Placement:
    """Return reqs whose VRAM floor is above every offer in ``base``."""
    return dataclasses.replace(_BASE_REQS, min_vram_gb=max(o.vram_gb for o in base) + 1)


def _above_every_cuda(base: list[Offer]) -> Placement:
    """Return reqs whose CUDA floor no real catalog entry can meet."""
    del base
    return dataclasses.replace(_BASE_REQS, min_cuda="99.0")


def _below_every_price(base: list[Offer]) -> Placement:
    """Return reqs whose price ceiling is below every offer in ``base``.

    A free catalog (LocalProvider bills nothing) needs a NEGATIVE ceiling to
    exclude anything — the honest consequence of ``filter_offers`` comparing
    ``cost > max``, not a contrived probe.
    """
    return dataclasses.replace(
        _BASE_REQS,
        max_usd_per_hr=min(o.cost_rate_usd_per_hr for o in base) - 0.01,
    )


def _filters(narrow: _Narrow, *, axis: str) -> _Proof:
    """Proof: ``find_offers`` drops the whole catalog once ``axis`` is narrowed."""

    def proof(provider_name: str) -> str:
        catalog = _CATALOGS[provider_name]
        base = catalog(_BASE_REQS)
        if not base:
            return "the offline catalog is empty, so this probe proves nothing"
        survivors = catalog(narrow(base))
        if survivors:
            return (
                f"{len(survivors)} of {len(base)} offer(s) survived a {axis} "
                "filter that excludes the entire catalog; find_offers is not "
                "applying it"
            )
        return ""

    return proof


def _orders_by_preference() -> _Proof:
    """Proof: ``find_offers`` promotes a named accelerator to the front."""

    def proof(provider_name: str) -> str:
        catalog = _CATALOGS[provider_name]
        base = catalog(_BASE_REQS)
        wanted = base[-1].gpu_type if base else ""
        if len(base) < 2 or base[0].gpu_type == wanted:
            return (
                "the offline catalog has fewer than two distinct accelerators, "
                "so an ordering probe proves nothing"
            )
        ranked = catalog(dataclasses.replace(_BASE_REQS, accelerators=(wanted,)))
        if not ranked or ranked[0].gpu_type != wanted:
            return (
                f"preferring {wanted!r} did not put it first; got "
                f"{[o.gpu_type for o in ranked][:3]}"
            )
        return ""

    return proof


# ---------------------------------------------------------------------------
# Observers
# ---------------------------------------------------------------------------

_PROBE_SCRIPT = "#!/bin/bash\necho kf-probe-provision-marker\n"
_PROBE_BUILD_SCRIPT = "#!/bin/bash\necho kf-probe-build-marker\n"
_PROBE_RUNTIME_SCRIPT = "#!/bin/bash\necho kf-probe-runtime-marker\n"
_PROBE_TAGS = {"kf_probe": "sentinel"}


def _runpod_input(launch: Launch) -> dict[str, Any]:
    """Return RunPod's ``variables.input`` dict."""
    return dict(launch.payload["input"])


def _runpod_env(launch: Launch) -> dict[str, str]:
    """Return RunPod's wire ``env`` list flattened to a dict."""
    return {e["key"]: e["value"] for e in _runpod_input(launch)["env"]}


def _runpod_provision_script(launch: Launch) -> str:
    """Return the provision script RunPod actually put on the wire.

    RunPod gzips-then-base64s the script into ``KINOFORGE_PROVISION_SCRIPT``
    (the ~101 KB env-payload ceiling), so observing it means undoing that
    encoding. Mirroring the encoding here is the price of observing the
    value rather than merely the presence of the key.
    """
    blob = _runpod_env(launch).get("KINOFORGE_PROVISION_SCRIPT", "")
    if not blob:
        return ""
    return gzip.decompress(base64.b64decode(blob)).decode("utf-8")


def _sky_task(launch: Launch) -> dict[str, Any]:
    """Return SkyPilot's task config."""
    return dict(launch.payload["task_config"])


def _sky_resources(launch: Launch) -> dict[str, Any]:
    """Return SkyPilot's ``resources`` block."""
    return dict(_sky_task(launch).get("resources", {}))


def _modal_request(launch: Launch) -> dict[str, Any]:
    """Return the ``ModalAppRequest`` fields."""
    return dict(launch.payload["request"])


def _probe_tag(launch: Launch) -> str | None:
    """Return the probe tag off the Instance the provider fabricated."""
    if launch.instance is None:
        return None
    return launch.instance.tags.get("kf_probe")


# ---------------------------------------------------------------------------
# The proof table
# ---------------------------------------------------------------------------

_WIRE_PROOFS: dict[str, dict[str, _Proof]] = {
    "runpod": {
        "image": _tracks(
            lambda ln: _runpod_input(ln)["imageName"],
            probe={"image": "kf-probe/image:sentinel"},
            expected="kf-probe/image:sentinel",
        ),
        # RunPod's proxy only serves ports carrying an explicit protocol
        # suffix, so a bare port is rewritten to "<port>/http" on the wire.
        "ports": _tracks(
            lambda ln: _runpod_input(ln)["ports"],
            probe={"ports": ("9999",)},
            expected="9999/http",
        ),
        "volume_gb": _tracks(
            lambda ln: _runpod_input(ln)["volumeInGb"],
            probe={"volume_gb": 77},
            expected=77,
        ),
        "volume_mount": _tracks(
            lambda ln: _runpod_input(ln)["volumeMountPath"],
            probe={"volume_mount": "/kf-probe-mount"},
            expected="/kf-probe-mount",
        ),
        "env": _tracks(
            lambda ln: _runpod_env(ln).get("KF_PROBE"),
            probe={"env": {"KF_PROBE": "sentinel"}},
            expected="sentinel",
        ),
        # Off-wire but load-bearing: spec.tags selects pod-vs-serverless and
        # becomes Instance.tags, which is what the ledger and the reaper see.
        "tags": _tracks(_probe_tag, probe={"tags": _PROBE_TAGS}, expected="sentinel"),
        "run_id": _tracks(
            lambda ln: _runpod_input(ln)["name"],
            probe={"run_id": "kf-probe-run"},
            expected="kf-probe-run",
        ),
        # S3: the engine emits steps + a launch and RunPod composes the script
        # from them, so these two are what reach the wire. Both proofs decode
        # the gzip+base64 blob — an envelope-only check would pass against a
        # script that never moved.
        "setup_steps": _tracks(
            lambda ln: "kf-probe-step-marker" in _runpod_provision_script(ln),
            probe={"setup_steps": (SpecSetupStep("echo kf-probe-step-marker"),)},
            expected=True,
        ),
        "launch": _tracks(
            lambda ln: (
                _runpod_provision_script(ln).rstrip().endswith("kf-probe-launch")
            ),
            probe={"launch": SpecLaunch(("kf-probe-launch",))},
            expected=True,
        ),
        # Lifecycle rides the wire inside the rendered self-terminator, which
        # is the only place RunPod can enforce it once the controller dies.
        "lifecycle": _tracks(
            lambda ln: "4242.0" in _runpod_env(ln).get("KINOFORGE_SELFTERM_SCRIPT", ""),
            probe={"lifecycle.idle_timeout_s": 4242.0},
            expected=True,
        ),
        "backend_options": _tracks_cfg(
            lambda ln: _runpod_input(ln)["cloudType"],
            backend_options={"runpod": {"cloud_type": "community"}},
            expected="COMMUNITY",
        ),
        "mode": _runpod_mode_selects_its_mutation(),
        "heartbeat_mode": _runpod_heartbeat_mode_selects_its_substrate(),
        "accelerators": _orders_by_preference(),
        "min_vram_gb": _filters(_above_every_vram, axis="min_vram_gb"),
        "min_cuda": _filters(_above_every_cuda, axis="min_cuda"),
        "max_usd_per_hr": _filters(_below_every_price, axis="max_usd_per_hr"),
    },
    "skypilot": {
        # sky rejects a bare image name unless a cloud is pinned, so the
        # provider normalises to the cloud-agnostic "docker:" form.
        "image": _tracks(
            lambda ln: _sky_resources(ln).get("image_id"),
            probe={"image": "kf-probe/image:sentinel"},
            expected="docker:kf-probe/image:sentinel",
        ),
        "env": _tracks(
            lambda ln: _sky_task(ln)["envs"].get("KF_PROBE"),
            probe={"env": {"KF_PROBE": "sentinel"}},
            expected="sentinel",
        ),
        # Observed on the pre-launch provisional row (F12). S5 moved the writer
        # from the provider to the orchestrator; the row is still the only
        # Instance a skypilot capture can see, because _StopLaunch aborts
        # inside sky.launch.
        "tags": _tracks(_probe_tag, probe={"tags": _PROBE_TAGS}, expected="sentinel"),
        "run_id": _tracks(
            lambda ln: ln.payload["launch_kwargs"]["cluster_name"],
            probe={"run_id": "kf-probe-run"},
            expected="kf-probe-run",
        ),
        # S3: the steps are Task.setup and the launch is Task.run. Observing
        # the two separately is the point — the bug this replaced was a
        # provider that could not tell them apart.
        "setup_steps": _tracks(
            lambda ln: "kf-probe-step-marker" in _sky_task(ln)["setup"],
            probe={"setup_steps": (SpecSetupStep("echo kf-probe-step-marker"),)},
            expected=True,
        ),
        "launch": _tracks(
            lambda ln: _sky_task(ln).get("run"),
            probe={"launch": SpecLaunch(("kf-probe-cmd", "--flag"))},
            expected="kf-probe-cmd --flag",
        ),
        "lifecycle": _tracks(
            lambda ln: ln.payload["launch_kwargs"]["idle_minutes_to_autostop"],
            probe={"lifecycle.idle_timeout_s": 4242.0},
            expected=70,
        ),
        # SkyPilot's namespace is read by the composition root and arrives as
        # constructor arguments, so the probe is at config level — the route
        # an operator actually uses.
        "backend_options": _tracks_cfg(
            lambda ln: _sky_resources(ln).get("cloud"),
            backend_options={"skypilot": {"clouds": ["kfprobe"]}},
            expected="kfprobe",
        ),
        # Like backend_options, region is pinned by the composition root
        # after construction, so the probe has to go through the config.
        "region": _tracks_cfg_placement(
            lambda ln: _sky_resources(ln).get("region"),
            placement={"region": "kf-probe-region"},
            expected="kf-probe-region",
        ),
        "spot": _tracks(
            lambda ln: _sky_resources(ln).get("use_spot"),
            probe={"placement.spot": True},
            expected=True,
        ),
        "accelerators": _orders_by_preference(),
        "min_vram_gb": _filters(_above_every_vram, axis="min_vram_gb"),
        "min_cuda": _filters(_above_every_cuda, axis="min_cuda"),
    },
    "modal": {
        "image": _tracks(
            lambda ln: _modal_request(ln)["image"],
            probe={"image": "kf-probe/image:sentinel"},
            expected="kf-probe/image:sentinel",
        ),
        "env": _tracks(
            lambda ln: _modal_request(ln)["env"].get("KF_PROBE"),
            probe={"env": {"KF_PROBE": "sentinel"}},
            expected="sentinel",
        ),
        "tags": _tracks(_probe_tag, probe={"tags": _PROBE_TAGS}, expected="sentinel"),
        "run_id": _tracks(
            lambda ln: _modal_request(ln)["run_id"],
            probe={"run_id": "kf-probe-run"},
            expected="kf-probe-run",
        ),
        # S3: Modal partitions the steps on their own flags. Two proofs,
        # because the partition has two sides and a provider that dropped
        # either one would still satisfy the other.
        "setup_steps": _tracks(
            lambda ln: (
                "kf-probe-runtime-marker"
                in (_modal_request(ln)["provision_script"] or ""),
                "kf-probe-build-marker"
                in (_modal_request(ln)["image_build_script"] or ""),
            ),
            probe={
                "setup_steps": (
                    SpecSetupStep("echo kf-probe-runtime-marker"),
                    SpecSetupStep(
                        "echo kf-probe-build-marker", bakeable=True, runtime=False
                    ),
                )
            },
            expected=(True, True),
        ),
        "launch": _tracks(
            lambda ln: _modal_request(ln)["launch_line"],
            probe={"launch": SpecLaunch(("kf-probe-cmd", "--flag"))},
            expected="kf-probe-cmd --flag",
        ),
        "volume_mount": _tracks(
            lambda ln: _modal_request(ln)["volume_mount"],
            probe={"volume_mount": "/kf-probe-mount"},
            expected="/kf-probe-mount",
        ),
        "lifecycle": _tracks(
            lambda ln: _modal_request(ln)["scaledown_window_s"],
            probe={"lifecycle.idle_timeout_s": 4242.0},
            expected=4242,
        ),
        "accelerators": _orders_by_preference(),
        "min_vram_gb": _filters(_above_every_vram, axis="min_vram_gb"),
        "min_cuda": _filters(_above_every_cuda, axis="min_cuda"),
    },
    "local": {
        "tags": _tracks(_probe_tag, probe={"tags": _PROBE_TAGS}, expected="sentinel"),
        "min_vram_gb": _filters(_above_every_vram, axis="min_vram_gb"),
        "min_cuda": _filters(_above_every_cuda, axis="min_cuda"),
        "max_usd_per_hr": _filters(_below_every_price, axis="max_usd_per_hr"),
    },
}

_PROOF_CASES = [
    (provider, field)
    for provider in sorted(_WIRE_PROOFS)
    for field in sorted(_WIRE_PROOFS[provider])
]


@pytest.mark.parametrize("provider_name", sorted(_EXPECTED_PROVIDERS))
def test_every_consumed_field_has_a_wire_proof(provider_name: str) -> None:
    """A CONSUMED claim with no proof is an unproven claim.

    Bug caught: a provider is flattered — a field it drops on the floor is
    declared CONSUMED — and nothing in the suite ever observes the wire.
    """
    cls = registry.provider_class(provider_name)
    assert cls is not None
    consumed = {f for f, s in cls.consumes().items() if s is FieldSupport.CONSUMED}
    proofs = set(_WIRE_PROOFS.get(provider_name, {}))
    unproven = consumed - proofs
    assert not unproven, (
        f"{provider_name} claims to consume {sorted(unproven)} with no wire "
        "proof; add one to _WIRE_PROOFS or declare the field UNSUPPORTED"
    )


@pytest.mark.parametrize("provider_name", sorted(_EXPECTED_PROVIDERS))
def test_no_proof_outlives_its_declaration(provider_name: str) -> None:
    """Every proof names a field this provider still declares CONSUMED.

    Bug caught: a declaration is downgraded to UNSUPPORTED (or the field is
    deleted) and its proof lingers, passing against a payload nobody reads
    any more and making the table look better covered than it is.
    """
    cls = registry.provider_class(provider_name)
    assert cls is not None
    consumed = {f for f, s in cls.consumes().items() if s is FieldSupport.CONSUMED}
    orphaned = set(_WIRE_PROOFS.get(provider_name, {})) - consumed
    assert not orphaned, (
        f"{provider_name} has wire proofs for {sorted(orphaned)}, which it no "
        "longer declares CONSUMED; delete the proof or restore the claim"
    )


@pytest.mark.parametrize(("provider_name", "field"), _PROOF_CASES)
def test_wire_proof_holds(provider_name: str, field: str) -> None:
    """The captured payload really does follow the field it claims to read.

    Bug caught: a provider stops reading a field (or starts hardcoding the
    value it used to read) — the payload no longer moves when the spec does,
    and this fails before an operator's setting silently stops applying.
    """
    why = _WIRE_PROOFS[provider_name][field](provider_name)
    assert not why, f"{provider_name}.{field}: {why}"


# ---------------------------------------------------------------------------
# The other direction: UNSUPPORTED means the wire really does not move
# ---------------------------------------------------------------------------

#: Placement knobs an operator can set that reach no provider's wire — the
#: F5 findings this plan exists to surface. Each entry is (field, probe).
#: Restricted to fields whose consumption would have to be visible in the
#: launch payload: the selection fields (min_vram_gb and friends) are applied
#: while enumerating offers, so their absence from a payload proves nothing.
_INERT_PLACEMENT_PROBES: list[tuple[str, Any]] = [
    ("accelerator_count", 4),
    ("disk_gb", 777),
    ("region", "kf-probe-region"),
    ("spot", True),
]


@pytest.mark.parametrize("provider_name", sorted(_EXPECTED_PROVIDERS))
def test_unsupported_placement_knobs_leave_the_payload_untouched(
    provider_name: str,
) -> None:
    """An UNSUPPORTED placement knob must genuinely change nothing.

    Bug caught (the flattery's mirror image): a provider is taught to read
    ``disk_gb`` or ``accelerator_count`` and the declaration is left saying
    UNSUPPORTED — Task 6 would then reject a value the provider now honours.
    """
    cls = registry.provider_class(provider_name)
    assert cls is not None
    declared = cls.consumes()
    baseline = _baseline(provider_name).payload
    for field, probe in _INERT_PLACEMENT_PROBES:
        if declared[field] is not FieldSupport.UNSUPPORTED:
            continue
        moved = _probed(
            provider_name, spec=_replaced({f"placement.{field}": probe})
        ).payload
        assert moved == baseline, (
            f"{provider_name} declares placement.{field} UNSUPPORTED but the "
            f"payload changed when it was set to {probe!r}; the provider now "
            "reads it — declare it CONSUMED and add a wire proof"
        )
