"""B7 T3: orchestrator-side hold_until_first_tick wire-in.

Verifies that deploy_session.__enter__ wraps step 8 (verify) + step 8.5
(pool + HeartbeatLoop.start) in hold_until_first_tick under the same gate
as HeartbeatLoop spawn (interval > 0 AND instance AND provider).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import kinoforge.engines.fake  # noqa: F401 — registers fake engine
import kinoforge.providers.local  # noqa: F401 — registers local provider
import kinoforge.sources.http  # noqa: F401 — registers https:// source
from kinoforge.core.config import Config, load_config
from kinoforge.core.interfaces import Instance, ModelProfile
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.orchestrator import deploy_session
from kinoforge.engines.fake import FakeEngine
from kinoforge.providers.local import LocalProvider
from kinoforge.stores.local import LocalArtifactStore

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

_HOSTED_YAML = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake-base.safetensors"
    kind: base
    target: diffusion_models
"""


def _compute_cfg(heartbeat_interval_s: float | None = None) -> Config:
    yaml = _COMPUTE_YAML
    if heartbeat_interval_s is not None:
        yaml = yaml.replace(
            "    budget: 1.0",
            f"    budget: 1.0\n    heartbeat_interval_s: {heartbeat_interval_s}",
        )
    return load_config(yaml)


def _hosted_cfg() -> Config:
    return load_config(_HOSTED_YAML)


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


class _HostedFakeEngine(FakeEngine):
    """FakeEngine variant that claims it does NOT require compute."""

    requires_compute: bool = False


def _make_hosted_engine() -> _HostedFakeEngine:
    return _HostedFakeEngine(
        probe_profile=_probe_profile(),
        declared_flags_map={},
        required_spec_keys=set(),
    )


def _seed_profile_cache(store: LocalArtifactStore, cfg: Config) -> None:
    from kinoforge.core.profiles import JsonProfileCache

    cache = JsonProfileCache(store)
    cache.warm(cfg.capability_key(), _probe_profile())


def test_compute_path_with_heartbeat_acquires_provision_lock(tmp_path: Path) -> None:
    """Cold-path deploy_session with heartbeat_interval_s > 0 holds
    provision:<id> from instance-available through HeartbeatLoop start.

    Bug catch: a forgotten ``hold_until_first_tick`` wrap would leave a
    naked window where a concurrent reaper could destroy the boot-mid pod.
    """
    cfg = _compute_cfg(heartbeat_interval_s=0.05)
    store = LocalArtifactStore(tmp_path)
    provider = LocalProvider()
    engine = _make_engine()
    _seed_profile_cache(store, cfg)

    lock_held_at_hb_start = [False]

    class _ProbeSpyHeartbeatLoop:
        def __init__(
            self,
            *,
            ledger: Ledger,
            provider: Any,
            instance_id: str,
            interval_s: float,
            **_unused: Any,
        ) -> None:
            self._ledger = ledger
            self._instance_id = instance_id

        def start(self) -> None:
            # Non-blocking acquire of provision:<id> from a sibling
            # FileLock instance. If the outer claim_ctx is holding the
            # lock, this acquire returns None.
            lock = store.acquire_lock(f"provision:{self._instance_id}", ttl_s=1.0)
            token = lock.acquire(blocking=False)
            if token is None:
                lock_held_at_hb_start[0] = True
            else:
                lock.release(token)
            # Land the heartbeat tick so the outer hold releases.
            self._ledger.record(
                Instance(
                    id=self._instance_id,
                    provider="local",
                    status="ready",
                    created_at=0.0,
                    cost_rate_usd_per_hr=0.0,
                    tags={},
                )
            )
            self._ledger.touch(self._instance_id, heartbeat_thread_tick=time.time())

        def stop(self) -> None:
            pass

    with deploy_session(
        cfg,
        store=store,
        provider=provider,
        engine=engine,
        heartbeat_loop_factory=_ProbeSpyHeartbeatLoop,
    ):
        pass

    assert lock_held_at_hb_start[0], (
        "provision:<id> must be held by deploy_session when HeartbeatLoop.start() runs"
    )


def test_hosted_path_does_not_acquire_provision_lock(tmp_path: Path) -> None:
    """Hosted-engine deploy_session (requires_compute=False) routes to
    nullcontext() and never acquires provision:<id>.

    Discriminating: regression sentry — were hold_until_first_tick to fire
    on the hosted path, the lookup would block forever (no HB ticks ever).
    Sidecar absence proves the gate skipped the helper.
    """
    cfg = _hosted_cfg()
    store = LocalArtifactStore(tmp_path)
    _seed_profile_cache(store, cfg)

    with deploy_session(cfg, store=store, engine=_make_hosted_engine()) as session:
        assert session.instance is None

    lock_files = list(tmp_path.rglob("provision*.lock"))
    assert lock_files == [], f"hosted path created provision lock files: {lock_files}"


def test_heartbeat_disabled_compute_path_does_not_acquire_provision_lock(
    tmp_path: Path,
) -> None:
    """When heartbeat_interval_s is None on a compute path, deploy_session
    routes to nullcontext() — classify will return HEARTBEAT_UNKNOWN
    (non-destructive) for these entries, so the race B7 closes doesn't
    exist on this branch."""
    cfg = _compute_cfg(heartbeat_interval_s=None)
    store = LocalArtifactStore(tmp_path)
    provider = LocalProvider()
    engine = _make_engine()
    _seed_profile_cache(store, cfg)

    with deploy_session(cfg, store=store, provider=provider, engine=engine):
        pass

    # Sidecar files persist across release since the FileLock unlink-race
    # fix (an empty payload means released/probed, a JSON payload means
    # held). The enforceable invariant — unchanged from the original
    # intent — is that no provision claim is HELD or leaked at exit.
    held = [p for p in tmp_path.rglob("provision*.lock") if p.stat().st_size > 0]
    assert held == [], f"HB-disabled path left a held provision claim: {held}"
