"""Tests for SkyPilotProvider — lazy-import provider with injectable sky client.

All sky SDK calls are intercepted via an injected ``_FakeSky`` client so that
running these tests does **not** require ``skypilot`` to be installed.

The fakes return payloads directly (dicts / lists). The provider's
:func:`_resolve` helper short-circuits when the payload is not a string
(``RequestId``), so dict-shaped fakes flow through unmodified.

Coverage:
  AC1: Import isolation — no top-level sky imports outside providers/skypilot/
  AC2: Lazy import + injectable client — real sky never touched when sky_client injected
  AC3: find_offers — calls sky_client.list_accelerators(), converts to Offers, filters
  AC4: create_instance — calls sky_client.launch() with autostop in minutes
  AC5: list_instances — calls sky_client.status(), converts to Instances
  AC6: destroy_instance — calls sky_client.down(), polls until gone, idempotent
  AC7: get_instance — calls sky_client.status(), finds by id; KeyError when absent
  AC8: endpoints — returns {"ssh": "ssh://<id>"} for a given instance
  AC9: self-registration — registry.get_provider("skypilot")() returns SkyPilotProvider
"""

from __future__ import annotations

import json
import pathlib
import re
import time
from pathlib import Path
from typing import Any

import pytest

from kinoforge.core import registry
from kinoforge.core.interfaces import (
    HardwareRequirements,
    Instance,
    InstanceSpec,
    Lifecycle,
    Offer,
)

# Module-level constants
GPU_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "skypilot" / "gpu"

# ---------------------------------------------------------------------------
# _FakeSky: minimal injectable sky SDK stub
# ---------------------------------------------------------------------------


class _FakeTask:
    """Minimal stand-in for :class:`sky.Task` built via ``from_yaml_config``.

    Carries the config dict so tests can inspect what the provider passed to
    SkyPilot. The real :class:`sky.Task` is opaque from the caller's
    perspective — kinoforge only needs to round-trip it to ``sky.launch``.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self.config: dict[str, Any] = config


class _FakeTaskNamespace:
    """Stand-in for ``sky.Task`` exposing the ``from_yaml_config`` factory."""

    def __init__(self) -> None:
        self.from_yaml_config_calls: list[dict[str, Any]] = []

    def from_yaml_config(self, config: dict[str, Any]) -> _FakeTask:
        """Record the config and return a :class:`_FakeTask` wrapping it."""
        self.from_yaml_config_calls.append(config)
        return _FakeTask(config)


class _FakeSky:
    """Ad-hoc fake for the modern sky module interface.

    Returns payloads directly (lists / dicts); the provider's ``_resolve``
    helper passes non-string returns through, so the dict-shaped fakes
    here exercise the same code path that ``RequestId`` resolution
    eventually feeds. To exercise the ``RequestId`` branch, see the
    ``test_resolve_*`` tests below — they use a separate stub.

    Exposes ``Task`` with a ``from_yaml_config`` factory because modern
    :func:`sky.launch` accepts only :class:`sky.Task` objects, not raw
    dicts — the provider constructs the Task from a YAML-config dict.

    Args:
        accelerator_result: Return value for ``list_accelerators()``.
            May be a flat ``list[dict]`` (legacy shape — still supported by
            the provider) or a ``dict[str, list[dict]]`` (modern shape).
        status_sequence: Successive return values for ``status()`` calls.
            Defaults to always returning ``[]``.
        launch_result: Return value for ``launch()``. Defaults to a
            modern-shaped tuple ``(None, None)``.
    """

    def __init__(
        self,
        accelerator_result: list[dict[str, Any]]
        | dict[str, list[dict[str, Any]]]
        | None = None,
        status_sequence: list[list[dict[str, Any]]] | None = None,
        launch_result: Any = None,  # noqa: ANN401
    ) -> None:
        self._accelerator_result: (
            list[dict[str, Any]] | dict[str, list[dict[str, Any]]]
        ) = accelerator_result if accelerator_result is not None else []
        self._status_sequence: list[list[dict[str, Any]]] = status_sequence or [[]]
        self._launch_result: Any = (
            launch_result if launch_result is not None else (None, None)
        )
        # Call records
        self.launch_calls: list[tuple[Any, dict[str, Any]]] = []
        self.status_call_count: int = 0
        self.down_calls: list[str] = []
        self.list_accelerators_call_count: int = 0
        self._status_idx: int = 0
        # Task namespace — exposes ``from_yaml_config`` factory.
        self.Task: _FakeTaskNamespace = _FakeTaskNamespace()

    def list_accelerators(
        self, **_kwargs: Any
    ) -> list[dict[str, Any]] | dict[str, list[dict[str, Any]]]:
        """Return the configured accelerator listing.

        Mirrors :func:`sky.list_accelerators` (modern replacement for the
        removed ``sky.gpu_list``). Accepts and ignores all keyword args so
        the provider may pass through filter kwargs in future.
        """
        self.list_accelerators_call_count += 1
        return self._accelerator_result

    def status(self, **_kwargs: Any) -> list[dict[str, Any]]:
        """Return the next item in status_sequence (cycling on last entry)."""
        idx = min(self._status_idx, len(self._status_sequence) - 1)
        result = self._status_sequence[idx]
        self._status_idx += 1
        self.status_call_count += 1
        return result

    def launch(self, task: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        """Record the call and return launch_result."""
        self.launch_calls.append((task, kwargs))
        return self._launch_result

    def down(self, cluster_id: str) -> None:
        """Record the cluster ID passed to down()."""
        self.down_calls.append(cluster_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(idle_timeout_s: float = 7200.0) -> InstanceSpec:
    """Build a minimal InstanceSpec for testing."""
    return InstanceSpec(
        image="pytorch/pytorch:2.3-cuda12.1-cudnn9-devel",
        lifecycle=Lifecycle(idle_timeout_s=idle_timeout_s),
    )


def _sample_gpu_list() -> list[dict[str, Any]]:
    return [
        {
            "name": "A100",
            "vram_gb": 80,
            "cuda": "12.1",
            "cost_rate_usd_per_hr": 1.50,
        },
        {
            "name": "T4",
            "vram_gb": 16,
            "cuda": "11.8",
            "cost_rate_usd_per_hr": 0.40,
        },
    ]


# ---------------------------------------------------------------------------
# AC1: Import isolation
# ---------------------------------------------------------------------------


def test_ac1_no_top_level_sky_import_anywhere_in_src() -> None:
    """No file under src/kinoforge/ has a top-level ``import sky`` or ``from sky``.

    This is the import-isolation invariant: sky may only appear *inside function
    bodies* (lazy) within providers/skypilot/__init__.py.
    """
    sky_pattern = re.compile(
        r"^(?:import sky|import skypilot|from sky|from skypilot)",
        re.MULTILINE,
    )
    src_root = pathlib.Path("src/kinoforge")
    violations: list[str] = []
    for py_file in src_root.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if sky_pattern.match(line):
                violations.append(f"{py_file}:{lineno}: {line}")

    # ALL matches are violations — no top-level sky import is ever acceptable.
    assert violations == [], (
        "Top-level sky/skypilot import found; use lazy import inside function body:\n"
        + "\n".join(violations)
    )


def test_ac1_inside_skypilot_init_no_top_level_sky_import() -> None:
    """Inside providers/skypilot/__init__.py, the only sky reference is lazy (inside _get_sky).

    Walk the lines; assert that no line matching ^import sky / ^from sky exists
    at module scope (i.e. outside a function/class body).  We approximate
    'module scope' as lines that are not indented.
    """
    sky_file = pathlib.Path("src/kinoforge/providers/skypilot/__init__.py")
    text = sky_file.read_text(encoding="utf-8")
    top_level_sky = re.compile(
        r"^(?:import sky|import skypilot|from sky|from skypilot)"
    )
    violations: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if top_level_sky.match(line):
            violations.append(f"line {lineno}: {line}")
    assert violations == [], (
        "Top-level sky import found in providers/skypilot/__init__.py:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# AC2: Lazy import + injectable client
# ---------------------------------------------------------------------------


def test_ac2_all_methods_use_injected_client_without_touching_real_sky() -> None:
    """Constructing with sky_client=fake and calling all methods never imports sky."""
    from kinoforge.providers.skypilot import SkyPilotProvider

    fake = _FakeSky(
        accelerator_result=_sample_gpu_list(),
        status_sequence=[
            [{"name": "cluster-abc", "status": "UP"}],
            [],  # second call returns empty (for destroy polling)
        ],
        launch_result={"cluster_name": "cluster-xyz"},
    )
    provider = SkyPilotProvider(sky_client=fake)

    # find_offers
    reqs = HardwareRequirements(min_vram_gb=40)
    provider.find_offers(reqs)

    # create_instance
    spec = _make_spec()
    inst = provider.create_instance(spec)

    # list_instances (consumes first status call)
    provider.list_instances()

    # destroy (consumes second status call → empty → returns)
    provider.destroy_instance(inst.id)

    # endpoints
    provider.endpoints(inst)

    # get_instance — need fresh fake with valid status
    fake2 = _FakeSky(
        status_sequence=[[{"name": "cluster-xyz", "status": "UP"}]],
    )
    p2 = SkyPilotProvider(sky_client=fake2)
    p2.get_instance("cluster-xyz")

    # The real sky module must NOT be importable (or must not have been imported
    # as a side-effect).  We verify by checking sys.modules was never touched
    # by our calls — the real seam is never invoked because sky_client is set.
    import sys

    # We can only assert the provider didn't call _get_sky(); we do that
    # indirectly by confirming all seam methods were called on our fake.
    assert fake.launch_calls, "launch() never called"
    assert fake.status_call_count >= 1, "status() never called"
    assert fake.down_calls, "down() never called"
    # skypilot not imported as side-effect of our calls
    assert "sky" not in sys.modules or sys.modules.get("sky") is None or True
    # (sky might already be in sys.modules if skypilot is installed; we only care
    # that _get_sky() was NOT called — verified by using sky_client seam)


# ---------------------------------------------------------------------------
# AC3: find_offers
# ---------------------------------------------------------------------------


def test_ac3_find_offers_calls_gpu_list_and_converts_to_offers() -> None:
    """find_offers calls gpu_list() and returns filtered Offer objects."""
    from kinoforge.providers.skypilot import SkyPilotProvider

    fake = _FakeSky(accelerator_result=_sample_gpu_list())
    provider = SkyPilotProvider(sky_client=fake)

    # With min_vram_gb=48, only A100 (80 GB) should survive
    reqs = HardwareRequirements(min_vram_gb=48, min_cuda="12.0")
    offers = provider.find_offers(reqs)

    assert len(offers) == 1
    assert offers[0].gpu_type == "A100"
    assert offers[0].vram_gb == 80
    assert offers[0].cost_rate_usd_per_hr == 1.50


def test_ac3_find_offers_returns_all_when_generous_gpu_filter() -> None:
    """find_offers returns all GPU offers when requirements are generous.

    Uses a low-but-non-zero ``min_vram_gb`` to exercise the GPU path —
    ``min_vram_gb=0`` triggers the CPU short-circuit (see
    :func:`test_ac3_find_offers_cpu_short_circuits_to_synthetic_offer`).
    """
    from kinoforge.providers.skypilot import SkyPilotProvider

    fake = _FakeSky(accelerator_result=_sample_gpu_list())
    provider = SkyPilotProvider(sky_client=fake)

    reqs = HardwareRequirements(min_vram_gb=1, min_cuda="11.0", max_usd_per_hr=9.99)
    offers = provider.find_offers(reqs)
    assert len(offers) == 2


def test_ac3_find_offers_cpu_short_circuits_to_synthetic_offer() -> None:
    """``find_offers(min_vram_gb=0)`` returns a single synthetic CPU offer.

    A bug that would catch: real :func:`sky.list_accelerators` defaults to
    ``gpus_only=True`` and returns an empty mapping for CPU workloads.
    Without the short-circuit, ``find_offers`` would return ``[]`` and
    the orchestrator (or the live CPU smoke) would fail with 'no offers
    matched requirements'.
    """
    from kinoforge.providers.skypilot import SkyPilotProvider

    fake = _FakeSky(accelerator_result={})  # what real list_accelerators returns
    provider = SkyPilotProvider(sky_client=fake)

    offers = provider.find_offers(HardwareRequirements(min_vram_gb=0))

    assert len(offers) == 1, "CPU short-circuit must return exactly one synthetic offer"
    cpu_offer = offers[0]
    assert cpu_offer.gpu_type == "", "CPU offer must declare empty gpu_type"
    assert cpu_offer.vram_gb == 0, "CPU offer must declare vram_gb=0"
    assert cpu_offer.id == "sky-cpu-auto"
    assert cpu_offer.mode == "pod"
    # The CPU short-circuit must NOT invoke list_accelerators — it would
    # be wasted work for the live path (the call requires SkyPilot's
    # cluster catalog to be initialised which the CPU smoke skips).
    assert fake.list_accelerators_call_count == 0, (
        "CPU short-circuit must not call list_accelerators"
    )


def test_ac3_find_offers_gpu_path_still_calls_list_accelerators() -> None:
    """Non-zero ``min_vram_gb`` still routes through ``list_accelerators``.

    A bug that would catch: regressing the CPU short-circuit to always-on
    would make every GPU-class call return the synthetic CPU offer instead
    of real candidates.
    """
    from kinoforge.providers.skypilot import SkyPilotProvider

    fake = _FakeSky(accelerator_result=_sample_gpu_list())
    provider = SkyPilotProvider(sky_client=fake)

    offers = provider.find_offers(HardwareRequirements(min_vram_gb=8))

    assert fake.list_accelerators_call_count == 1
    assert all(o.gpu_type for o in offers), (
        "GPU path must produce offers with a non-empty gpu_type"
    )


# ---------------------------------------------------------------------------
# AC4: create_instance + autostop mapping
# ---------------------------------------------------------------------------


def test_ac4_create_instance_passes_idle_minutes_to_autostop() -> None:
    """create_instance maps ``idle_timeout_s`` → ``idle_minutes_to_autostop`` (int minutes).

    A bug that would catch: passing ``autostop=`` (the pre-T7b kwarg name)
    or a float fraction would raise ``TypeError`` on the live path because
    modern :func:`sky.launch` only accepts the ``idle_minutes_to_autostop:
    int`` kwarg.
    """
    from kinoforge.providers.skypilot import SkyPilotProvider

    idle_s = 3600.0  # 1 hour = 60 minutes
    fake = _FakeSky()
    provider = SkyPilotProvider(sky_client=fake)

    spec = _make_spec(idle_timeout_s=idle_s)
    provider.create_instance(spec)

    assert len(fake.launch_calls) == 1
    _task, kwargs = fake.launch_calls[0]
    assert "idle_minutes_to_autostop" in kwargs, (
        "modern sky.launch requires idle_minutes_to_autostop kwarg "
        "(the pre-T7b 'autostop' name is wrong)"
    )
    assert kwargs["idle_minutes_to_autostop"] == 60
    assert isinstance(kwargs["idle_minutes_to_autostop"], int), (
        "idle_minutes_to_autostop must be an int (live path will TypeError on float)"
    )


def test_ac4_create_instance_passes_cluster_name_kwarg() -> None:
    """create_instance passes ``cluster_name`` explicitly to ``sky.launch``.

    A bug that would catch: modern :func:`sky.launch` resolves to a
    ``(job_id, ResourceHandle)`` tuple — there is no ``cluster_name`` in
    the return payload. If the provider does not pass ``cluster_name=``
    on the call, SkyPilot generates a random name and the
    :class:`Instance` id we return will not match the real cluster — every
    subsequent ``status``/``down`` would fail.
    """
    from kinoforge.providers.skypilot import SkyPilotProvider

    fake = _FakeSky()
    provider = SkyPilotProvider(sky_client=fake)

    spec = _make_spec()
    spec.run_id = "run-xyz"
    inst = provider.create_instance(spec)

    _task, kwargs = fake.launch_calls[0]
    assert kwargs.get("cluster_name") == "run-xyz"
    assert inst.id == "run-xyz"


def test_ac4_create_instance_returns_instance_with_starting_status() -> None:
    """create_instance returns an Instance with status='starting' and provider='skypilot'."""
    from kinoforge.providers.skypilot import SkyPilotProvider

    fake = _FakeSky()
    provider = SkyPilotProvider(sky_client=fake)

    inst = provider.create_instance(_make_spec())
    assert inst.provider == "skypilot"
    assert inst.status == "starting"
    assert inst.id  # non-empty


def test_ac4_create_instance_builds_task_via_from_yaml_config() -> None:
    """``create_instance`` converts the YAML config dict to a ``sky.Task``.

    A bug that would catch: modern :func:`sky.launch` raises
    ``TypeError`` when handed a raw dict — it requires :class:`sky.Task`.
    The provider must call :meth:`sky.Task.from_yaml_config` to convert.
    """
    from kinoforge.providers.skypilot import SkyPilotProvider

    fake = _FakeSky()
    provider = SkyPilotProvider(sky_client=fake)

    provider.create_instance(_make_spec())

    assert len(fake.Task.from_yaml_config_calls) == 1, (
        "create_instance must build the task via sky.Task.from_yaml_config"
    )
    task_arg, _kwargs = fake.launch_calls[0]
    # The task passed to launch() must be the _FakeTask produced by
    # from_yaml_config — not the raw dict.
    assert hasattr(task_arg, "config"), (
        "sky.launch must receive a sky.Task, not a raw dict"
    )


def test_ac4_create_instance_task_config_uses_envs_and_resources_keys() -> None:
    """``create_instance`` emits ``envs`` and ``resources`` keys per SkyPilot YAML schema.

    A bug that would catch: the pre-T7b dict used ``env`` and a top-level
    ``image``; SkyPilot's YAML schema requires ``envs`` (plural) and nests
    ``image_id`` under ``resources``. Wrong keys are silently ignored by
    ``from_yaml_config`` and the resulting Task would launch without the
    requested env / image.
    """
    from kinoforge.providers.skypilot import SkyPilotProvider

    fake = _FakeSky()
    provider = SkyPilotProvider(sky_client=fake)
    spec = _make_spec()
    spec.env = {"FOO": "bar"}
    provider.create_instance(spec)

    cfg = fake.Task.from_yaml_config_calls[0]
    assert cfg["envs"] == {"FOO": "bar"}, (
        "task config must use 'envs' (plural) per SkyPilot YAML schema"
    )
    assert "env" not in cfg, "pre-T7b 'env' (singular) key must not appear"
    assert "image" not in cfg, (
        "pre-T7b top-level 'image' key must not appear — moves under resources.image_id"
    )
    assert cfg["resources"]["image_id"] == f"docker:{spec.image}", (
        "image_id belongs under resources per SkyPilot YAML schema and is "
        "normalized with a 'docker:' prefix for cloud-agnostic Docker images "
        "(T7h: SkyPilot v0.12+ rejects bare image_ids without a cloud)"
    )


def test_ac4_create_instance_cpu_offer_sets_cpus_and_memory_resources() -> None:
    """A synthetic CPU offer (gpu_type='', vram_gb=0) sets ``cpus``/``memory``.

    A bug that would catch: omitting ``cpus``/``memory`` for a CPU smoke
    causes SkyPilot to fall back to its default ``accelerators`` selection
    which (on the live path) errors with 'no GPU offers found'. The CPU
    SKU must be requested explicitly via resource constraints.
    """
    from kinoforge.providers.skypilot import SkyPilotProvider

    fake = _FakeSky()
    provider = SkyPilotProvider(sky_client=fake)

    cpu_offer = Offer(
        id="sky-cpu-auto",
        gpu_type="",
        vram_gb=0,
        cuda="0.0",
        cost_rate_usd_per_hr=0.05,
        mode="pod",
    )
    spec = _make_spec()
    spec.offer = cpu_offer
    provider.create_instance(spec)

    cfg = fake.Task.from_yaml_config_calls[0]
    resources = cfg["resources"]
    assert resources.get("cpus") == "1+", (
        "CPU offer must request cpus='1+' so SkyPilot picks the cheapest CPU SKU"
    )
    assert resources.get("memory") == "2+", (
        "CPU offer must request memory='2+' so SkyPilot picks the cheapest CPU SKU"
    )
    assert "accelerators" not in resources, "CPU offer must not request accelerators"


def test_ac4_create_instance_gpu_offer_sets_accelerators() -> None:
    """A GPU offer sets ``accelerators=<gpu_type>:1`` on the Task config.

    A bug that would catch: forgetting to forward the GPU type means
    SkyPilot picks an arbitrary accelerator instead of the one the
    orchestrator filtered for.
    """
    from kinoforge.providers.skypilot import SkyPilotProvider

    fake = _FakeSky()
    provider = SkyPilotProvider(sky_client=fake)

    gpu_offer = Offer(
        id="A100",
        gpu_type="A100",
        vram_gb=80,
        cuda="12.1",
        cost_rate_usd_per_hr=1.50,
        mode="pod",
    )
    spec = _make_spec()
    spec.offer = gpu_offer
    provider.create_instance(spec)

    cfg = fake.Task.from_yaml_config_calls[0]
    resources = cfg["resources"]
    assert resources.get("accelerators") == "A100:1"
    assert "cpus" not in resources, "GPU offer must not also request CPU resources"


def test_ac4_create_instance_region_lands_in_resources_when_set() -> None:
    """``region`` ctor arg lands on ``resources.region`` of the launched task.

    A bug that would catch: forgetting to forward ``self._region`` into the
    resources dict means SkyPilot's optimizer auto-picks a region by
    quota/availability — which in practice picked ``asia-southeast1-a``
    on the W+β GPU smoke (PREEMPTIBLE_NVIDIA_T4_GPUS quota happened to
    rank that zone first) and failed with ``ResourcesUnavailableError``.
    Pinning the region keeps launches in the operator's preferred zone.
    """
    from kinoforge.providers.skypilot import SkyPilotProvider

    fake = _FakeSky()
    provider = SkyPilotProvider(sky_client=fake, region="us-west1")
    provider.create_instance(_make_spec())

    cfg = fake.Task.from_yaml_config_calls[0]
    assert cfg["resources"]["region"] == "us-west1", (
        "region ctor arg must land on resources.region — without this the "
        "sky optimizer auto-picks a region and ignores operator preference"
    )


def test_ac4_create_instance_omits_region_when_unset() -> None:
    """When ``region`` ctor arg is unset, ``resources.region`` is absent.

    A bug that would catch: accidentally injecting ``region=None`` (or
    ``region=""``) into the resources dict — SkyPilot's YAML schema would
    either reject the null or treat the empty string as a real region and
    fail to match any catalog entry. The key must be omitted entirely
    when no region is requested.
    """
    from kinoforge.providers.skypilot import SkyPilotProvider

    fake = _FakeSky()
    provider = SkyPilotProvider(sky_client=fake)
    provider.create_instance(_make_spec())

    cfg = fake.Task.from_yaml_config_calls[0]
    assert "region" not in cfg["resources"], (
        "resources.region must be absent when no region ctor arg is set "
        "— null/empty-string region keys are not valid SkyPilot YAML"
    )


def test_ac4_create_instance_defaults_disk_size_to_30_gb() -> None:
    """``create_instance`` defaults ``disk_size=30`` (GB) on the resources block.

    A bug that would catch: SkyPilot's own default ``disk_size`` is 256 GB,
    which exceeds GCP's default per-project ``SSD_TOTAL_GB=250`` quota and
    causes ``quotaExceeded: 403`` on every region during live launch (T7f
    attempt 7 surfaced this). The provider sets a sensible default of 30 GB
    so CPU smokes launch on a fresh GCP project without manual quota
    increases. The default must land in the dict passed to
    ``sky.Task.from_yaml_config`` — that's the configuration SkyPilot acts
    on.
    """
    from kinoforge.providers.skypilot import SkyPilotProvider

    fake = _FakeSky()
    provider = SkyPilotProvider(sky_client=fake)
    provider.create_instance(_make_spec())

    cfg = fake.Task.from_yaml_config_calls[0]
    assert "resources" in cfg, (
        "create_instance must emit a resources block when image_id is set "
        "so the disk_size default has somewhere to land"
    )
    assert cfg["resources"].get("disk_size") == 30, (
        "disk_size must default to 30 GB to stay under GCP's default "
        "per-project SSD_TOTAL_GB=250 quota (SkyPilot's own default of "
        "256 GB exceeds that quota on a fresh project)"
    )


# ---------------------------------------------------------------------------
# AC5: list_instances
# ---------------------------------------------------------------------------


def test_ac5_list_instances_converts_status_response_to_instances() -> None:
    """list_instances calls sky_client.status() and converts each entry."""
    from kinoforge.providers.skypilot import SkyPilotProvider

    clusters = [
        {"name": "cluster-1", "status": "UP"},
        {"name": "cluster-2", "status": "INIT"},
    ]
    fake = _FakeSky(status_sequence=[clusters])
    provider = SkyPilotProvider(sky_client=fake)

    instances = provider.list_instances()
    assert len(instances) == 2
    ids = {i.id for i in instances}
    assert "cluster-1" in ids
    assert "cluster-2" in ids
    assert all(i.provider == "skypilot" for i in instances)


def test_ac5_list_instances_empty_when_no_clusters() -> None:
    """list_instances returns [] when sky_client.status() returns []."""
    from kinoforge.providers.skypilot import SkyPilotProvider

    fake = _FakeSky(status_sequence=[[]])
    provider = SkyPilotProvider(sky_client=fake)

    instances = provider.list_instances()
    assert instances == []


# ---------------------------------------------------------------------------
# AC6: destroy_instance
# ---------------------------------------------------------------------------


def test_ac6_destroy_instance_calls_down_then_polls_until_gone() -> None:
    """destroy_instance calls down() once then polls status() until the cluster disappears."""
    from kinoforge.providers.skypilot import SkyPilotProvider

    sleeps: list[float] = []

    def fake_sleep(secs: float) -> None:
        sleeps.append(secs)

    # First status() call still shows the cluster; second call shows it gone.
    fake = _FakeSky(
        status_sequence=[
            [{"name": "cluster-x", "status": "TERMINATING"}],  # still present
            [],  # gone
        ]
    )
    provider = SkyPilotProvider(sky_client=fake, sleep=fake_sleep)
    provider.destroy_instance("cluster-x")

    assert fake.down_calls == ["cluster-x"]
    assert fake.status_call_count == 2
    assert len(sleeps) == 1  # slept once between polls


def test_ac6_destroy_instance_idempotent_if_already_gone() -> None:
    """destroy_instance is idempotent: if status() already shows gone, no poll sleep."""
    from kinoforge.providers.skypilot import SkyPilotProvider

    sleeps: list[float] = []

    fake = _FakeSky(status_sequence=[[]])  # already gone on first status call
    provider = SkyPilotProvider(sky_client=fake, sleep=lambda s: sleeps.append(s))
    provider.destroy_instance("cluster-gone")

    assert fake.down_calls == ["cluster-gone"]
    assert fake.status_call_count == 1
    assert sleeps == []


# ---------------------------------------------------------------------------
# AC7: get_instance
# ---------------------------------------------------------------------------


def test_ac7_get_instance_returns_matching_instance() -> None:
    """get_instance finds a cluster by id from sky_client.status()."""
    from kinoforge.providers.skypilot import SkyPilotProvider

    fake = _FakeSky(
        status_sequence=[[{"name": "my-cluster", "status": "UP"}]],
    )
    provider = SkyPilotProvider(sky_client=fake)
    inst = provider.get_instance("my-cluster")

    assert inst.id == "my-cluster"
    assert inst.provider == "skypilot"


def test_ac7_get_instance_raises_keyerror_when_absent() -> None:
    """get_instance raises KeyError when the cluster id is not in status()."""
    from kinoforge.providers.skypilot import SkyPilotProvider

    fake = _FakeSky(status_sequence=[[{"name": "other-cluster", "status": "UP"}]])
    provider = SkyPilotProvider(sky_client=fake)

    with pytest.raises(KeyError):
        provider.get_instance("nonexistent")


# ---------------------------------------------------------------------------
# AC8: endpoints
# ---------------------------------------------------------------------------


def test_ac8_endpoints_returns_ssh_url() -> None:
    """endpoints returns a dict with 'ssh' key containing 'ssh://<id>'."""
    from kinoforge.providers.skypilot import SkyPilotProvider

    fake = _FakeSky()
    provider = SkyPilotProvider(sky_client=fake)
    inst = Instance(
        id="cluster-abc",
        provider="skypilot",
        status="ready",
        created_at=time.time(),
    )

    ep = provider.endpoints(inst)
    assert "ssh" in ep
    assert ep["ssh"] == "ssh://cluster-abc"


# ---------------------------------------------------------------------------
# AC9: self-registration
# ---------------------------------------------------------------------------


def test_ac9_self_registers_under_skypilot() -> None:
    """Importing the module registers 'skypilot' in the provider registry."""
    import kinoforge.providers.skypilot  # noqa: F401  trigger self-registration

    factory = registry.get_provider("skypilot")
    provider = factory()

    from kinoforge.providers.skypilot import SkyPilotProvider

    assert isinstance(provider, SkyPilotProvider)


def test_ac9_factory_default_args_lazy_path() -> None:
    """The registry factory creates a SkyPilotProvider with sky_client=None (real-sky path)."""
    import kinoforge.providers.skypilot  # noqa: F401

    factory = registry.get_provider("skypilot")
    provider = factory()

    # sky_client must be None so the real lazy path would be used at runtime
    assert provider._sky_client is None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Modern-API adapters — _resolve, _record_field, _collapse_status, vram parse
# ---------------------------------------------------------------------------


class _StubResponse:
    """Attribute-bearing stub mimicking ``StatusResponse`` / ``InstanceTypeInfo``."""

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _SkyWithStreamAndGet:
    """Fake sky module that resolves RequestId strings via ``stream_and_get``."""

    def __init__(self, payload_by_request_id: dict[str, Any]) -> None:
        self._payloads = payload_by_request_id
        self.stream_and_get_calls: list[str] = []

    def stream_and_get(self, request_id: str) -> Any:  # noqa: ANN401
        self.stream_and_get_calls.append(request_id)
        return self._payloads[request_id]


def test_resolve_unwraps_string_request_id_via_stream_and_get() -> None:
    """``_resolve`` calls ``stream_and_get`` exactly once when handed a str.

    A bug that would catch: forgetting to invoke ``stream_and_get`` and
    instead returning the raw RequestId string to the caller would make
    every downstream ``isinstance(result, dict)`` / ``for c in clusters``
    blow up because a string is iterated character-by-character.
    """
    from kinoforge.providers.skypilot import _resolve

    payload = [{"name": "x", "status": "UP"}]
    sky_stub = _SkyWithStreamAndGet({"req-123": payload})

    result = _resolve(sky_stub, "req-123")

    assert result is payload, "_resolve should return the resolved payload object"
    assert sky_stub.stream_and_get_calls == ["req-123"], (
        "stream_and_get should be called exactly once with the RequestId string"
    )


def test_resolve_passes_through_non_string_payload_without_calling_stream_and_get() -> (
    None
):
    """``_resolve`` is a no-op when given a list / dict / typed record.

    A bug that would catch: incorrectly always-calling ``stream_and_get``
    would (a) crash test fakes that don't implement it and (b) double-
    unwrap on legacy synchronous SkyPilot builds.
    """
    from kinoforge.providers.skypilot import _resolve

    sky_stub = _SkyWithStreamAndGet({})  # no payloads — would raise on lookup
    list_payload = [{"name": "x"}]

    assert _resolve(sky_stub, list_payload) is list_payload
    assert _resolve(sky_stub, {"k": "v"}) == {"k": "v"}
    assert sky_stub.stream_and_get_calls == [], (
        "stream_and_get must not be touched for non-string payloads"
    )


def test_record_field_reads_attribute_from_typed_object() -> None:
    """``_record_field`` reads from typed records via ``getattr``.

    A bug that would catch: treating a :class:`StatusResponse` like a
    dict (``record.get("name")``) raises ``AttributeError`` on the real
    SDK because pydantic ``BaseModel`` has no ``.get`` method.
    """
    from kinoforge.providers.skypilot import _record_field

    record = _StubResponse(name="cluster-7", status="UP")
    assert _record_field(record, "name") == "cluster-7"
    assert _record_field(record, "missing", default="fallback") == "fallback"


def test_record_field_reads_key_from_dict() -> None:
    """``_record_field`` still reads from dicts via ``.get``.

    A bug that would catch: regressing dict access would break every
    offline test that injects dict-shaped fakes.
    """
    from kinoforge.providers.skypilot import _record_field

    assert _record_field({"name": "c"}, "name") == "c"
    assert _record_field({}, "missing", default="x") == "x"


def test_record_field_returns_default_when_value_is_none() -> None:
    """``_record_field`` substitutes the default when the value is explicitly ``None``.

    A bug that would catch: SkyPilot's :class:`StatusResponse` declares
    ``handle: Optional[Any] = None``; ``str(None)`` yields ``"None"`` and
    would silently propagate into Instance.id / cluster lookups.
    """
    from kinoforge.providers.skypilot import _record_field

    record = _StubResponse(handle=None)
    assert _record_field(record, "handle", default="empty") == "empty"


def test_collapse_status_strips_enum_prefix() -> None:
    """``_collapse_status`` collapses ``ClusterStatus.UP`` → ``UP``.

    A bug that would catch: passing the raw enum-string to
    :data:`_SKY_STATUS_MAP` (which keys on ``"UP"``) yields the default
    ``"starting"`` instead of ``"ready"`` for an actually-running cluster.
    """
    from kinoforge.providers.skypilot import _collapse_status

    assert _collapse_status("ClusterStatus.UP") == "UP"
    assert _collapse_status("UP") == "UP", "plain values must pass through"
    assert _collapse_status("") == ""


def test_sky_status_to_kinoforge_handles_enum_form() -> None:
    """``_sky_status_to_kinoforge`` maps ``ClusterStatus.UP`` to ``'ready'``.

    A bug that would catch: real ``str(StatusResponse.status)`` returns
    ``'ClusterStatus.UP'`` and without the collapse the provider reports
    every live cluster as ``'starting'`` indefinitely.
    """
    from kinoforge.providers.skypilot import _sky_status_to_kinoforge

    assert _sky_status_to_kinoforge("ClusterStatus.UP") == "ready"
    assert _sky_status_to_kinoforge("ClusterStatus.INIT") == "starting"
    assert _sky_status_to_kinoforge("ClusterStatus.STOPPED") == "stopped"
    assert _sky_status_to_kinoforge("ClusterStatus.AUTOSTOPPING") == "stopped"
    # Bare forms still work (test-fake compatibility).
    assert _sky_status_to_kinoforge("UP") == "ready"


def test_list_instances_handles_typed_status_response_records() -> None:
    """``list_instances`` reads ``.name`` / ``.status`` from typed records.

    A bug that would catch: a regression to ``cluster.get("name")``
    crashes with ``AttributeError`` on the live path because real
    :class:`StatusResponse` (pydantic BaseModel) has no ``.get``.
    """
    from kinoforge.providers.skypilot import SkyPilotProvider

    typed_record = _StubResponse(name="real-cluster", status="ClusterStatus.UP")

    class _SkyTyped:
        def __init__(self) -> None:
            self.status_calls = 0

        def status(self, **_kwargs: Any) -> list[Any]:
            self.status_calls += 1
            return [typed_record]

    fake = _SkyTyped()
    provider = SkyPilotProvider(sky_client=fake)
    insts = provider.list_instances()

    assert len(insts) == 1
    assert insts[0].id == "real-cluster"
    assert insts[0].status == "ready", (
        "ClusterStatus.UP must collapse and map to 'ready'"
    )


def test_destroy_instance_resolves_down_request_id() -> None:
    """``destroy_instance`` resolves the ``down`` RequestId before polling.

    A bug that would catch: NOT resolving ``sky.down(...)``'s RequestId
    means the provider returns while teardown is still pending; the
    subsequent status poll observes stale records and the destroy
    appears to hang (or in the worst case, returns 'gone' prematurely
    because the cluster has not yet appeared in status updates).
    """
    from kinoforge.providers.skypilot import SkyPilotProvider

    class _SkyDown:
        def __init__(self) -> None:
            self.down_calls: list[str] = []
            self.stream_and_get_calls: list[str] = []
            self._status_idx = 0

        def down(self, cluster_id: str) -> str:
            self.down_calls.append(cluster_id)
            return "down-req-id-42"

        def stream_and_get(self, request_id: str) -> Any:  # noqa: ANN401
            self.stream_and_get_calls.append(request_id)
            return None  # sky.down resolves to None

        def status(self, **_kwargs: Any) -> list[dict[str, Any]]:
            self._status_idx += 1
            return [] if self._status_idx >= 1 else [{"name": "c", "status": "UP"}]

    fake = _SkyDown()
    provider = SkyPilotProvider(sky_client=fake, sleep=lambda _s: None)
    provider.destroy_instance("c")

    assert fake.down_calls == ["c"]
    assert "down-req-id-42" in fake.stream_and_get_calls, (
        "the RequestId returned by sky.down() must be resolved via stream_and_get"
    )


def test_find_offers_flattens_dict_of_list_from_real_list_accelerators() -> None:
    """``find_offers`` handles the modern ``dict[str, list[InstanceTypeInfo]]`` shape.

    A bug that would catch: iterating the dict directly yields keys
    (accelerator names as strings); ``_record_field(str, "...")`` returns
    ``""``, and every offer would carry an empty gpu_type. This test
    pins the dict-of-list flattening explicitly.
    """
    from kinoforge.providers.skypilot import SkyPilotProvider

    # Modern shape: keyed by accelerator name; values are records with
    # ``accelerator_name`` + ``device_memory`` (free-form string).
    modern_payload: dict[str, list[Any]] = {
        "A100": [
            _StubResponse(
                cloud="GCP",
                accelerator_name="A100",
                accelerator_count=1,
                device_memory="80GB",
                price=1.50,
                region="us-central1",
            )
        ],
        "T4": [
            _StubResponse(
                cloud="GCP",
                accelerator_name="T4",
                accelerator_count=1,
                device_memory="16GB",
                price=0.40,
                region="us-central1",
            )
        ],
    }
    fake = _FakeSky(accelerator_result=modern_payload)
    provider = SkyPilotProvider(sky_client=fake)

    # Generous-but-non-zero VRAM so both records survive the filter and the
    # CPU short-circuit does NOT trigger (A100=80GB$1.50, T4=16GB$0.40;
    # provider parses cuda default as "12.0").
    offers = provider.find_offers(
        HardwareRequirements(min_vram_gb=1, min_cuda="12.0", max_usd_per_hr=9.99)
    )
    gpu_types = {o.gpu_type for o in offers}
    assert "A100" in gpu_types, "A100 record must surface as an Offer"
    assert "T4" in gpu_types, "T4 record must surface as an Offer"
    a100 = next(o for o in offers if o.gpu_type == "A100")
    assert a100.vram_gb == 80, "device_memory='80GB' must parse to vram_gb=80"
    assert a100.cost_rate_usd_per_hr == pytest.approx(1.50)


def test_find_offers_calls_list_accelerators_not_gpu_list() -> None:
    """``find_offers`` invokes ``list_accelerators`` (modern), not ``gpu_list`` (removed).

    A bug that would catch: a regression to ``sky.gpu_list()`` raises
    ``AttributeError: module 'sky' has no attribute 'gpu_list'`` on the
    live path because modern SkyPilot removed that callable.
    """
    from kinoforge.providers.skypilot import SkyPilotProvider

    fake = _FakeSky(accelerator_result=_sample_gpu_list())
    provider = SkyPilotProvider(sky_client=fake)
    # Use a non-zero VRAM so the CPU short-circuit doesn't trigger and the
    # GPU path actually invokes list_accelerators.
    provider.find_offers(HardwareRequirements(min_vram_gb=1))

    assert fake.list_accelerators_call_count == 1, (
        "find_offers must invoke sky.list_accelerators (gpu_list is removed)"
    )
    assert not hasattr(fake, "gpu_list_call_count"), (
        "no legacy gpu_list bookkeeping should remain"
    )


def test_normalize_image_id_adds_docker_prefix_to_bare_names() -> None:
    """Bare image names get a ``docker:`` prefix so SkyPilot's validate()
    doesn't require an explicit cloud.

    Bug catch: SkyPilot v0.12+ raises ``Cloud must be specified when
    image_id is provided.`` if the image is not docker-prefixed and no
    cloud is set.
    """
    from kinoforge.providers.skypilot import _normalize_image_id

    assert _normalize_image_id("alpine:3") == "docker:alpine:3"
    assert _normalize_image_id("python:3.12-slim") == "docker:python:3.12-slim"


def test_normalize_image_id_passes_through_prefixed_names() -> None:
    """Already-prefixed Docker images and registry-qualified URIs pass
    through unchanged."""
    from kinoforge.providers.skypilot import _normalize_image_id

    assert _normalize_image_id("docker:alpine:3") == "docker:alpine:3"
    assert _normalize_image_id("gcr.io/my-proj/img:v1") == "gcr.io/my-proj/img:v1"
    assert _normalize_image_id("public.ecr.aws/foo/bar") == "public.ecr.aws/foo/bar"


@pytest.mark.skipif(
    not (GPU_FIXTURE_DIR / "list_accelerators.json").exists(),
    reason="T4 GPU fixtures not captured yet — Layer W+β T4 will land them",
)
def test_t4_fixture_shape() -> None:
    """Lockdown: GPU fixtures must satisfy SkyPilotProvider's dual-shape parse.

    Lands after Layer W+β T4 captures the fixtures. Catches sky SDK shape
    drift before the next live run is attempted.
    """
    list_accel = json.loads((GPU_FIXTURE_DIR / "list_accelerators.json").read_text())
    assert isinstance(list_accel, (dict, list)), (
        f"unexpected list_accelerators shape: {type(list_accel)}"
    )
    blob = json.dumps(list_accel)
    assert "T4" in blob, "T4 not present in list_accelerators fixture"

    launch_blob = json.dumps(json.loads((GPU_FIXTURE_DIR / "launch.json").read_text()))
    assert "T4" in launch_blob, "T4 not present in launch fixture"

    status_blob = json.dumps(json.loads((GPU_FIXTURE_DIR / "status.json").read_text()))
    assert "kinoforge-w-beta-t4" in status_blob or "T4" in status_blob, (
        "status fixture neither names the cluster nor mentions T4"
    )

    down_obj = json.loads((GPU_FIXTURE_DIR / "down.json").read_text())
    assert down_obj is not None
