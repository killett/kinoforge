"""Layer U T3: deploy_session spawns HeartbeatLoop when configured.

Tests assert the wire BEFORE the implementation lands. Once GREEN, the
contract is:

- ``cfg.lifecycle().heartbeat_interval_s is None`` (or compute=None /
  instance=None): no loop is constructed; the factory spy is never called.
- ``heartbeat_interval_s > 0`` with a compute instance: factory is called
  once, ``start()`` runs before ``yield``, ``stop()`` runs inside the
  ``finally`` block (even when the ``with`` body raises).
- ``heartbeat_loop_factory`` kwarg lets tests substitute a spy so we
  never depend on real thread scheduling for AC9-AC10.
- End-to-end with the real :class:`HeartbeatLoop`: the ledger entry
  gains ``last_heartbeat`` + ``heartbeat_thread_tick`` after a few ticks.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

import kinoforge.engines.fake  # noqa: F401 — registers fake engine
import kinoforge.providers.local  # noqa: F401 — registers local provider
import kinoforge.sources.http  # noqa: F401 — registers https:// source
from kinoforge.core.config import Config, load_config
from kinoforge.core.interfaces import (
    Instance,
    InstanceSpec,
    ModelProfile,
)
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.orchestrator import deploy_session
from kinoforge.engines.fake import FakeEngine
from kinoforge.providers.local import LocalProvider
from kinoforge.stores.local import LocalArtifactStore

# ---------------------------------------------------------------------------
# Minimal fixtures
# ---------------------------------------------------------------------------

_COMPUTE_YAML = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake-base.safetensors"
    kind: base
    target: diffusion_models
compute:
  provider: local
  image: fake:latest
  lifecycle:
    budget: 1.0
"""


def _compute_cfg(heartbeat_interval_s: float | None = None) -> Config:
    yaml = _COMPUTE_YAML
    if heartbeat_interval_s is not None:
        yaml = yaml.replace(
            "    budget: 1.0",
            f"    budget: 1.0\n    heartbeat_interval_s: {heartbeat_interval_s}",
        )
    return load_config(yaml)


def _probe_profile() -> ModelProfile:
    return ModelProfile(
        name="fake",
        max_frames=16,
        fps=8,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=False,
        supports_joint_audio=False,
    )


def _make_engine() -> FakeEngine:
    return FakeEngine(
        probe_profile=_probe_profile(),
        declared_flags_map={},
        required_spec_keys=set(),
    )


def _seed_profile_cache(store: LocalArtifactStore, cfg: Config) -> None:
    """Warm the profile cache so deploy_session hits the cache-hit branch."""
    from kinoforge.core.profiles import JsonProfileCache

    cache = JsonProfileCache(store)
    cache.warm(cfg.capability_key(), _probe_profile())


# ---------------------------------------------------------------------------
# Spy HeartbeatLoop factory
# ---------------------------------------------------------------------------


class _SpyHeartbeatLoop:
    """Replacement for HeartbeatLoop that records lifecycle calls.

    Structurally compatible with the HeartbeatLoop constructor — built
    by a factory the orchestrator calls with named kwargs.
    """

    def __init__(
        self,
        *,
        ledger: Any,
        provider: Any,
        instance_id: str,
        interval_s: float,
        **_unused: Any,
    ) -> None:
        self.ledger = ledger
        self.provider = provider
        self.instance_id = instance_id
        self.interval_s = interval_s
        self.events: list[str] = []

    def start(self) -> None:
        self.events.append("start")
        # B7 — mimic real HeartbeatLoop: ensure the ledger entry exists
        # and write a heartbeat_thread_tick so deploy_session's
        # hold_until_first_tick polling phase observes a fresh tick.
        # Without this the cooperative session-claim lock would
        # FirstTickTimeout (boot_timeout_s + 2*interval) on every spy
        # test that uses a positive interval.
        self.ledger.record(
            Instance(
                id=self.instance_id,
                provider="local",
                status="ready",
                created_at=0.0,
                cost_rate_usd_per_hr=0.0,
                tags={},
            )
        )
        self.ledger.touch(self.instance_id, heartbeat_thread_tick=time.time())

    def stop(self) -> None:
        self.events.append("stop")


class _SpyFactory:
    """Captures every HeartbeatLoop construction call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.loops: list[_SpyHeartbeatLoop] = []

    def __call__(self, **kwargs: Any) -> _SpyHeartbeatLoop:
        self.calls.append(dict(kwargs))
        loop = _SpyHeartbeatLoop(**kwargs)
        self.loops.append(loop)
        return loop


# ---------------------------------------------------------------------------
# AC9 — interval=None: no spawn
# ---------------------------------------------------------------------------


def test_deploy_session_with_interval_none_does_not_spawn_loop(
    tmp_path: Path,
) -> None:
    """heartbeat_interval_s = None (default) → factory never invoked.

    Bug catch: a forgotten ``if interval is not None`` guard would
    spawn the loop for every existing YAML config — adding a thread
    and ledger writes nobody asked for. Backwards-compat fence.
    """
    cfg = _compute_cfg(heartbeat_interval_s=None)
    store = LocalArtifactStore(tmp_path)
    provider = LocalProvider()
    engine = _make_engine()
    _seed_profile_cache(store, cfg)
    factory = _SpyFactory()

    with deploy_session(
        cfg,
        store=store,
        provider=provider,
        engine=engine,
        heartbeat_loop_factory=factory,
    ):
        pass

    assert factory.calls == []
    assert factory.loops == []


# ---------------------------------------------------------------------------
# AC9 — interval > 0: spawn, start, stop in order
# ---------------------------------------------------------------------------


def test_deploy_session_with_interval_spawns_starts_and_stops_loop_in_order(
    tmp_path: Path,
) -> None:
    """heartbeat_interval_s > 0 with compute → factory + start() then stop().

    Asserts the canonical lifecycle: the loop is instantiated before the
    yield, ``start()`` runs immediately, and ``stop()`` runs inside the
    finally so a clean exit always tears the thread down.
    """
    cfg = _compute_cfg(heartbeat_interval_s=30.0)
    store = LocalArtifactStore(tmp_path)
    provider = LocalProvider()
    engine = _make_engine()
    _seed_profile_cache(store, cfg)
    factory = _SpyFactory()

    with deploy_session(
        cfg,
        store=store,
        provider=provider,
        engine=engine,
        heartbeat_loop_factory=factory,
    ):
        # Inside the body, the loop has already started.
        assert len(factory.loops) == 1
        assert factory.loops[0].events == ["start"]

    # After the with block exits, stop() must have been called.
    assert factory.loops[0].events == ["start", "stop"]


def test_deploy_session_loop_factory_receives_expected_kwargs(
    tmp_path: Path,
) -> None:
    """Factory receives the ledger, provider, instance_id, and interval_s.

    Discriminating: pins down the contract the orchestrator commits to.
    A future refactor that forgets to pass ``interval_s`` would silently
    fall back to a default and either spam the ledger or never tick.
    """
    cfg = _compute_cfg(heartbeat_interval_s=15.0)
    store = LocalArtifactStore(tmp_path)
    provider = LocalProvider()
    engine = _make_engine()
    _seed_profile_cache(store, cfg)
    factory = _SpyFactory()

    with deploy_session(
        cfg,
        store=store,
        provider=provider,
        engine=engine,
        heartbeat_loop_factory=factory,
    ):
        pass

    assert len(factory.calls) == 1
    call = factory.calls[0]
    assert call["interval_s"] == 15.0
    assert call["provider"] is provider
    assert isinstance(call["instance_id"], str) and call["instance_id"]
    assert isinstance(call["ledger"], Ledger)


# ---------------------------------------------------------------------------
# AC10 — finally even on exception
# ---------------------------------------------------------------------------


def test_deploy_session_exit_stops_loop_even_when_body_raises(
    tmp_path: Path,
) -> None:
    """``with`` body raising still triggers loop.stop() in the finally.

    Bug catch: stop() guarded by a normal-exit assumption would leak the
    thread on every error-path exit and (per Layer 2 sentinel design)
    quickly look like a silent thread crash to any downstream consumer.
    """
    cfg = _compute_cfg(heartbeat_interval_s=30.0)
    store = LocalArtifactStore(tmp_path)
    provider = LocalProvider()
    engine = _make_engine()
    _seed_profile_cache(store, cfg)
    factory = _SpyFactory()

    class _SyntheticBoom(Exception):
        pass

    with pytest.raises(_SyntheticBoom):
        with deploy_session(
            cfg,
            store=store,
            provider=provider,
            engine=engine,
            heartbeat_loop_factory=factory,
        ):
            raise _SyntheticBoom("caller raised mid-session")

    assert factory.loops[0].events == ["start", "stop"]


# ---------------------------------------------------------------------------
# AC: end-to-end with real HeartbeatLoop
# ---------------------------------------------------------------------------


def test_deploy_session_writes_last_heartbeat_to_ledger_end_to_end(
    tmp_path: Path,
) -> None:
    """Real HeartbeatLoop ticks; ledger entry gains last_heartbeat + sentinel.

    Pre-records the instance to the ledger (deploy_session does not
    record; that's the CLI's job today). The loop then writes the
    persisted heartbeat fields and the status read path lights up.
    """
    cfg = _compute_cfg(heartbeat_interval_s=0.05)
    store = LocalArtifactStore(tmp_path)
    engine = _make_engine()
    _seed_profile_cache(store, cfg)
    ledger = Ledger(store=store)

    # Pre-record a placeholder; we'll discover the real id below and
    # touch a second record after deploy_session creates the instance.
    seen_instance: list[Instance] = []

    class _IDCaptureProvider(LocalProvider):
        def create_instance(self, spec: InstanceSpec) -> Instance:
            inst = super().create_instance(spec)
            seen_instance.append(inst)
            ledger.record(inst)
            return inst

    capture = _IDCaptureProvider()

    def _poll_entry(*, require_keys: tuple[str, ...]) -> dict[str, Any]:
        """Poll the ledger until ``require_keys`` all appear on the entry.

        Tolerates ``json.JSONDecodeError`` mid-poll: ``HeartbeatLoop``
        writes via ``LocalArtifactStore.put_bytes`` → ``Path.write_bytes``,
        which is non-atomic on APFS (truncate, then write). A concurrent
        reader can observe the zero-byte window and raise. Treat that as
        "not yet written" and retry.
        """
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                entries = Ledger(store=store).entries()
            except json.JSONDecodeError:
                time.sleep(0.05)
                continue
            entry = next(
                (e for e in entries if e.get("id") == seen_instance[0].id), None
            )
            if entry is not None and all(k in entry for k in require_keys):
                return entry
            time.sleep(0.05)
        pytest.fail(f"ledger entry never gained {require_keys} within 5s")

    with deploy_session(
        cfg,
        store=store,
        provider=capture,
        engine=engine,
    ):
        assert len(seen_instance) == 1
        # Wait for the first heartbeat write.
        _poll_entry(require_keys=("last_heartbeat",))

    # After exit, the entry must have both keys. Re-poll defensively in
    # case the macOS APFS write-visibility lag straddles the join.
    entry = _poll_entry(require_keys=("last_heartbeat", "heartbeat_thread_tick"))
    assert "last_heartbeat" in entry
    assert "heartbeat_thread_tick" in entry
