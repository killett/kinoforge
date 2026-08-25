"""Capacity-wait retry: re-query offers + retry create on CapacityError.

Compute-seam S1 made the window provider-scoped: it is sourced from
``compute.backend_options.runpod.capacity_wait_s`` and threaded from the
composition root down to :func:`_create_with_capacity_wait`, rather than
read off ``Lifecycle`` by every provider alike.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from kinoforge.core import orchestrator
from kinoforge.core.errors import CapacityError
from kinoforge.core.orchestrator import _create_with_capacity_wait

if TYPE_CHECKING:
    from pathlib import Path


class _Clock:
    def __init__(self, times: list[float]) -> None:
        self._times = times
        self._i = 0

    def now(self) -> float:
        t = self._times[min(self._i, len(self._times) - 1)]
        self._i += 1
        return t


def test_retries_then_succeeds() -> None:
    # Bug caught: a transient capacity miss fails the whole run instead of
    # riding the ~seconds-to-minutes drought RunPod recovers from.
    query_calls = {"n": 0}

    def find_offers() -> list[str]:
        query_calls["n"] += 1
        return ["offer"]  # non-empty

    attempts = {"n": 0}

    def create(_offers: list[str]) -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise CapacityError("no capacity")
        return "instance-ok"

    result = _create_with_capacity_wait(
        find_offers=find_offers,
        create=create,
        capacity_wait_s=300.0,
        retry_interval_s=25.0,
        clock=_Clock([0.0, 10.0, 20.0, 30.0]),
        sleep=lambda _s: None,
    )
    assert result == "instance-ok"
    assert attempts["n"] == 3
    assert query_calls["n"] == 3  # re-queried offers each attempt


def test_zero_wait_fails_on_first_miss() -> None:
    # Bug caught: capacity_wait=0 (smoke) still hangs retrying.
    def create(_offers: list[str]) -> str:
        raise CapacityError("no capacity")

    with pytest.raises(CapacityError):
        _create_with_capacity_wait(
            find_offers=lambda: ["offer"],
            create=create,
            capacity_wait_s=0.0,
            retry_interval_s=25.0,
            clock=_Clock([0.0, 1.0]),
            sleep=lambda _s: None,
        )


def test_deadline_exceeded_reraises() -> None:
    # Bug caught: an infinite loop when capacity never returns.
    def create(_offers: list[str]) -> str:
        raise CapacityError("still no capacity")

    with pytest.raises(CapacityError):
        _create_with_capacity_wait(
            find_offers=lambda: ["offer"],
            create=create,
            capacity_wait_s=60.0,
            retry_interval_s=25.0,
            clock=_Clock([0.0, 30.0, 61.0, 62.0]),
            sleep=lambda _s: None,
        )


def test_non_capacity_error_propagates() -> None:
    # Bug caught: a hard create error (auth/schema) is swallowed as retryable.
    def create(_offers: list[str]) -> str:
        raise RuntimeError("bad schema")

    with pytest.raises(RuntimeError):
        _create_with_capacity_wait(
            find_offers=lambda: ["offer"],
            create=create,
            capacity_wait_s=300.0,
            retry_interval_s=25.0,
            clock=_Clock([0.0, 10.0]),
            sleep=lambda _s: None,
        )


# ---------------------------------------------------------------------------
# The window must survive the trip from cfg to the retry loop.
# ---------------------------------------------------------------------------

_YAML = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake-base.safetensors"
    kind: base
    target: diffusion_models
compute:
  provider: {provider}
  image: fake:latest
  warm_reuse_auto_attach: false
{options}\
  lifecycle:
    budget: 1.0
"""


def _window_seen_by_generate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, provider: str, options: str
) -> float:
    """Drive the real ``generate()`` and report the window the retry loop got.

    Args:
        tmp_path: Per-test scratch dir (config + artifact store).
        monkeypatch: Used to spy on ``_create_with_capacity_wait``.
        provider: ``compute.provider`` value for the generated YAML.
        options: Extra ``compute:`` YAML lines (already indented).

    Returns:
        The ``capacity_wait_s`` the retry loop was invoked with.
    """
    from kinoforge.core.config import load_config
    from kinoforge.core.interfaces import GenerationRequest, ModelProfile
    from kinoforge.engines.fake import FakeEngine
    from kinoforge.providers.local import LocalProvider
    from kinoforge.stores.local import LocalArtifactStore

    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(_YAML.format(provider=provider, options=options))
    cfg = load_config(cfg_path)

    seen: list[float] = []
    real = orchestrator._create_with_capacity_wait

    def _spy(**kwargs: Any) -> Any:
        seen.append(kwargs["capacity_wait_s"])
        return real(**kwargs)

    monkeypatch.setattr(orchestrator, "_create_with_capacity_wait", _spy)
    orchestrator.generate(
        cfg,
        GenerationRequest(prompt="a sunset", mode="t2v"),
        store=LocalArtifactStore(tmp_path),
        provider=LocalProvider(),
        engine=FakeEngine(
            probe_profile=ModelProfile(
                name="fake",
                max_frames=16,
                fps=8,
                supported_modes={"t2v"},
                max_resolution=(512, 512),
                supports_native_extension=False,
                supports_joint_audio=False,
            ),
            declared_flags_map={},
            required_spec_keys=set(),
        ),
    )
    assert seen, "the capacity-wait loop was never reached"
    return seen[0]


def test_runpod_capacity_window_reaches_the_retry_loop_through_generate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RunPod cfg's capacity_wait_s arrives at the retry loop intact.

    Bug caught: ``generate`` is left on ``deploy_session``'s ``0.0``
    default after the window stops living on ``Lifecycle``. Every RunPod
    create then fails on the FIRST capacity miss — invisible in tests, a
    failed run during any real capacity drought.
    """
    window = _window_seen_by_generate(
        tmp_path,
        monkeypatch,
        provider="runpod",
        options="  backend_options:\n    runpod:\n      capacity_wait_s: 42\n",
    )
    assert window == 42.0


def test_non_runpod_provider_reaches_the_retry_loop_with_no_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Intended S1 behaviour change: skypilot no longer wraps create in the loop.

    Bug caught: the RunPod-shaped retry is preserved "just in case" for
    every provider, so a SkyPilot launch that cannot be satisfied burns
    the whole window before failing — on top of sky's own
    ``retry_until_up``, which is its real equivalent.
    """
    window = _window_seen_by_generate(
        tmp_path, monkeypatch, provider="skypilot", options=""
    )
    assert window == 0.0


# ---------------------------------------------------------------------------
# A caller that never mentions the keyword must still get its cfg's window.
#
# `0.0` is a legal window, not a sentinel, so a default of 0.0 turns every
# forgotten keyword into a silent loss of RunPod's capacity retry. That is
# exactly what happened to tools/capture_object_info.py, which calls
# `_provision_instance_and_build_backend` directly and went from a 300 s
# window to 0 s without a single test noticing.
# ---------------------------------------------------------------------------


def _window_seen_by_provision_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, provider: str
) -> float:
    """Call the provision helper the way an out-of-tree tool does.

    Deliberately omits ``capacity_wait_s`` — that omission IS the test.

    Args:
        tmp_path: Per-test scratch dir (config + artifact store).
        monkeypatch: Used to spy on ``_create_with_capacity_wait``.
        provider: ``compute.provider`` value for the generated YAML.

    Returns:
        The ``capacity_wait_s`` the retry loop was invoked with.
    """
    from kinoforge.core.config import load_config
    from kinoforge.core.interfaces import ModelProfile
    from kinoforge.engines.fake import FakeEngine
    from kinoforge.providers.local import LocalProvider
    from kinoforge.stores.local import LocalArtifactStore

    options = (
        "  backend_options:\n    runpod:\n      capacity_wait_s: 7\n"
        if provider == "runpod"
        else ""
    )
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(_YAML.format(provider=provider, options=options))
    cfg = load_config(cfg_path)

    seen: list[float] = []
    real = orchestrator._create_with_capacity_wait

    def _spy(**kwargs: Any) -> Any:
        seen.append(kwargs["capacity_wait_s"])
        return real(**kwargs)

    monkeypatch.setattr(orchestrator, "_create_with_capacity_wait", _spy)
    orchestrator._provision_instance_and_build_backend(
        resolved_engine=FakeEngine(
            probe_profile=ModelProfile(
                name="fake",
                max_frames=16,
                fps=8,
                supported_modes={"t2v"},
                max_resolution=(512, 512),
                supports_native_extension=False,
                supports_joint_audio=False,
            ),
            declared_flags_map={},
            required_spec_keys=set(),
        ),
        resolved_provider=LocalProvider(),
        cfg=cfg,
        run_id="run",
        key=cfg.capability_key(),
        creds=None,
        store=LocalArtifactStore(tmp_path),
        state_dir=tmp_path,
        for_discovery=True,
    )
    assert seen, "the capacity-wait loop was never reached"
    return seen[0]


def test_provision_helper_derives_the_window_when_the_caller_omits_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unthreaded caller still gets its cfg's RunPod window, not 0.0.

    Bug caught: exactly the tools/capture_object_info.py regression — a
    direct caller of the provision helper that never learned about the new
    keyword silently drops from a 300 s capacity window to "abort on the
    first miss", on a tool whose whole purpose is to walk the same
    provisioning path production walks.
    """
    assert (
        _window_seen_by_provision_helper(tmp_path, monkeypatch, provider="runpod")
        == 7.0
    )


def test_provision_helper_derives_zero_for_a_non_runpod_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deriving must not hand every provider a RunPod-shaped retry loop.

    Bug caught: the None-resolution ignores the provider and returns a
    blanket 300 s, quietly undoing the S1 scoping change for skypilot and
    modal.
    """
    assert (
        _window_seen_by_provision_helper(tmp_path, monkeypatch, provider="skypilot")
        == 0.0
    )


def test_an_explicit_zero_still_disables_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0.0 passed on purpose must survive the None-resolution.

    Bug caught: the resolution is written as a falsy test (``if not
    capacity_wait_s``) rather than an ``is None`` test, so a caller that
    deliberately disables retry silently gets the cfg default instead.
    """
    from kinoforge.core.config import load_config
    from kinoforge.core.interfaces import ModelProfile
    from kinoforge.engines.fake import FakeEngine
    from kinoforge.providers.local import LocalProvider
    from kinoforge.stores.local import LocalArtifactStore

    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        _YAML.format(
            provider="runpod",
            options="  backend_options:\n    runpod:\n      capacity_wait_s: 7\n",
        )
    )
    cfg = load_config(cfg_path)

    seen: list[float] = []
    real = orchestrator._create_with_capacity_wait

    def _spy(**kwargs: Any) -> Any:
        seen.append(kwargs["capacity_wait_s"])
        return real(**kwargs)

    monkeypatch.setattr(orchestrator, "_create_with_capacity_wait", _spy)
    orchestrator._provision_instance_and_build_backend(
        resolved_engine=FakeEngine(
            probe_profile=ModelProfile(
                name="fake",
                max_frames=16,
                fps=8,
                supported_modes={"t2v"},
                max_resolution=(512, 512),
                supports_native_extension=False,
                supports_joint_audio=False,
            ),
            declared_flags_map={},
            required_spec_keys=set(),
        ),
        resolved_provider=LocalProvider(),
        cfg=cfg,
        run_id="run",
        key=cfg.capability_key(),
        creds=None,
        store=LocalArtifactStore(tmp_path),
        state_dir=tmp_path,
        for_discovery=True,
        capacity_wait_s=0.0,
    )
    assert seen == [0.0]
