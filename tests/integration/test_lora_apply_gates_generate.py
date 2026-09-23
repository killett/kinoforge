"""Behavior: a LoRA that did not load must cost an error, not a video.

This is the defect the whole design exists to remove. Before this seam a
``loras:`` block aimed at a pod that ignores it produced a plausible
clip with no LoRA in it and nothing anywhere said so. These tests pin
the gate at its single choke point — ``deploy_session``, the one place
both ``generate()`` and ``batch_generate()`` funnel through — so the
guarantee holds for cold pods and caller-supplied warm pods alike.

If ``test_failed_apply_prevents_generation`` ever goes green while
generation proceeds, the feature is worse than not having it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# Import providers/engines/sources so they self-register.
import kinoforge.engines.fake  # noqa: F401
import kinoforge.providers.local  # noqa: F401
import kinoforge.sources.huggingface  # noqa: F401
from kinoforge.cli._main import _build_parser
from kinoforge.cli.loras_arg import parse_loras_heredoc
from kinoforge.core import orchestrator
from kinoforge.core.cancel import CancelToken
from kinoforge.core.config import Config, load_config
from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.errors import LoraFormatUnsupportedError
from kinoforge.core.grid.executor import _build_swap_generate_cmd, _ResolvedCell
from kinoforge.core.grid.spec import LoraStackEntry
from kinoforge.core.interfaces import (
    CredentialProvider,
    GenerationJob,
    Instance,
    ModelProfile,
)
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.lora import LoraEntry
from kinoforge.core.orchestrator import deploy_session
from kinoforge.core.pool import ConcurrentPool
from kinoforge.engines.fake import FakeBackend, FakeEngine
from kinoforge.providers.local import LocalProvider
from kinoforge.stores.local import LocalArtifactStore

_CFG_HEAD = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake-base.safetensors"
    kind: base
    target: diffusion_models
"""

_CFG_TAIL = """\
compute:
  provider: local
  image: fake:latest
  warm_reuse_auto_attach: false
  lifecycle:
    budget: 1.0
"""

_HOSTED_TAIL = ""


class _NullCreds(CredentialProvider):
    """Credential provider that knows nothing — keeps the run hermetic."""

    def get(self, key: str) -> str | None:
        del key
        return None


class _LoraSpyBackend(FakeBackend):
    """FakeBackend plus a LoRA surface, recording the call order.

    The shared ``events`` list is what proves ordering: the gate is only
    real if ``set_lora_stack`` is recorded before any ``submit``.
    """

    def __init__(
        self,
        probe: ModelProfile,
        *,
        events: list[str],
        set_stack_raises: Exception | None = None,
    ) -> None:
        super().__init__(probe=probe)
        self.events = events
        self.set_stack_calls: list[tuple[str, list[LoraEntry], dict[str, Any]]] = []
        self.submit_calls: list[GenerationJob] = []
        self._raises = set_stack_raises

    def set_lora_stack(
        self,
        *,
        pod_id: str,
        active_stack: list[LoraEntry],
        download_specs: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        self.set_stack_calls.append((pod_id, list(active_stack), dict(download_specs)))
        self.events.append("set_lora_stack")
        if self._raises is not None:
            raise self._raises
        return {
            "inventory": [
                {"ref": r.ref, "filename": f"{i}.safetensors", "size_bytes": 4096}
                for i, r in enumerate(active_stack)
            ],
            "free_bytes": 12345,
        }

    def submit(
        self, job: GenerationJob, *, cancel_token: CancelToken | None = None
    ) -> str:
        self.submit_calls.append(job)
        self.events.append("submit")
        return super().submit(job, cancel_token=cancel_token)


class _LoraSpyEngine(FakeEngine):
    """FakeEngine whose backends are :class:`_LoraSpyBackend` spies."""

    def __init__(self, *, set_stack_raises: Exception | None = None) -> None:
        super().__init__(
            probe_profile=_probe(), declared_flags_map={}, required_spec_keys=set()
        )
        self.events: list[str] = []
        self.backends: list[_LoraSpyBackend] = []
        self._raises = set_stack_raises

    def backend(
        self, instance: Instance | None, cfg: dict[str, object]
    ) -> _LoraSpyBackend:
        del instance, cfg
        made = _LoraSpyBackend(
            self._probe, events=self.events, set_stack_raises=self._raises
        )
        self.backends.append(made)
        return made


class _HostedLoraSpyEngine(_LoraSpyEngine):
    """A LoRA-capable spy that claims it needs no compute.

    Used to prove the hosted branch is chosen by the ABSENCE of
    ``set_lora_stack``, not by ``requires_compute`` — so an engine that
    grows a LoRA surface later is not silently skipped.
    """

    requires_compute: bool = False


class _HostedPlainEngine(FakeEngine):
    """Hosted FakeEngine — its backend has no LoRA surface at all."""

    requires_compute: bool = False

    def __init__(self) -> None:
        super().__init__(
            probe_profile=_probe(), declared_flags_map={}, required_spec_keys=set()
        )


def _probe() -> ModelProfile:
    return ModelProfile(
        name="fake",
        max_frames=16,
        fps=8,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=False,
        supports_joint_audio=False,
    )


def _cfg(lora_refs: list[str], *, hosted: bool = False) -> Config:
    block = ""
    if lora_refs:
        lines = "\n".join(f'  - ref: "{r}"\n    strength: 0.7' for r in lora_refs)
        block = f"loras:\n{lines}\n"
    tail = _HOSTED_TAIL if hosted else _CFG_TAIL
    return load_config(_CFG_HEAD + block + tail)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_failed_apply_prevents_generation(tmp_path: Path) -> None:
    """A refused stack raises out of deploy_session; submit never runs.

    Bug caught: swallowing the apply failure into a warning. The run
    would then complete and hand the operator a clip that looks right
    and contains none of the LoRAs they asked for — undetectable
    without frame-by-frame comparison against a known-good render.
    """
    boom = LoraFormatUnsupportedError(
        pod_id="p1", ref="hf:org/repo:a.safetensors", hint="use lightx2v/..."
    )
    engine = _LoraSpyEngine(set_stack_raises=boom)
    store = LocalArtifactStore(tmp_path)

    with pytest.raises(LoraFormatUnsupportedError):
        with deploy_session(
            _cfg(["hf:org/repo:a.safetensors"]),
            store=store,
            engine=engine,
            provider=LocalProvider(),
            creds=_NullCreds(),
            run_id="r",
        ) as session:
            session.pool.submit(GenerationJob(spec={}, params={}, segments=[]))

    assert engine.backends, "deploy_session must have built a backend"
    assert engine.backends[-1].submit_calls == [], (
        "a stack that did not load must never reach generation"
    )


def test_failed_apply_still_closes_the_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failing apply must not leak the session's worker pool.

    Bug caught: hooking the apply BEFORE the ``try:`` that owns the
    pool's ``finally``. Every LoRA failure would then strand a
    ThreadPoolExecutor (and the heartbeat thread) for the life of the
    process — for the batch CLI, the whole run — and skip the
    ``session_end`` ledger write, leaving concurrent scanners to treat
    the pod as busy until its claim TTL expires.
    """
    created: list[_SpyPool] = []

    class _SpyPool(ConcurrentPool):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0
            created.append(self)

        def close(
            self, *, cancel_pending: bool = False, timeout: float | None = None
        ) -> None:
            self.close_calls += 1
            super().close(cancel_pending=cancel_pending, timeout=timeout)

    monkeypatch.setattr(orchestrator, "ConcurrentPool", _SpyPool)

    engine = _LoraSpyEngine(
        set_stack_raises=LoraFormatUnsupportedError(
            pod_id="p1", ref="hf:org/repo:a.safetensors", hint="nope"
        )
    )
    store = LocalArtifactStore(tmp_path)

    with pytest.raises(LoraFormatUnsupportedError):
        with deploy_session(
            _cfg(["hf:org/repo:a.safetensors"]),
            store=store,
            engine=engine,
            provider=LocalProvider(),
            creds=_NullCreds(),
            run_id="r",
        ):
            pass

    assert created, "deploy_session must have built a pool before applying"
    assert created[-1].close_calls == 1, (
        "the pool built for this session must be closed even when the LoRA apply raises"
    )


def test_empty_stack_issues_no_http_call(tmp_path: Path) -> None:
    """Runs without LoRAs are untouched.

    Bug caught: a ``set_stack`` POST on every run, which against a
    server build with no LoRA surface is a 404 that breaks runs working
    today.
    """
    engine = _LoraSpyEngine()
    store = LocalArtifactStore(tmp_path)

    with deploy_session(
        _cfg([]),
        store=store,
        engine=engine,
        provider=LocalProvider(),
        creds=_NullCreds(),
        run_id="r",
    ) as session:
        assert session.backend is not None

    assert engine.backends[-1].set_stack_calls == []
    assert engine.events == []


def test_stack_is_applied_before_the_first_submit(tmp_path: Path) -> None:
    """The apply must precede generation, not race it.

    Bug caught: hooking the apply into the ``finally`` or after the
    ``yield``, which would order the POST after the first job and
    produce a LoRA-less first clip in every batch.
    """
    engine = _LoraSpyEngine()
    store = LocalArtifactStore(tmp_path)

    with deploy_session(
        _cfg(["hf:org/repo:a.safetensors"]),
        store=store,
        engine=engine,
        provider=LocalProvider(),
        creds=_NullCreds(),
        run_id="r",
    ) as session:
        session.pool.submit(GenerationJob(spec={}, params={}, segments=[])).result(
            timeout=5.0
        )

    assert engine.events[:2] == ["set_lora_stack", "submit"]
    backend = engine.backends[-1]
    assert len(backend.set_stack_calls) == 1
    pod_id, active_stack, specs = backend.set_stack_calls[0]
    assert pod_id, "the applied stack must be addressed at a real pod id"
    assert [e.ref for e in active_stack] == ["hf:org/repo:a.safetensors"]
    assert set(specs) == {"hf:org/repo:a.safetensors"}


def test_the_applied_inventory_reaches_the_real_ledger_row(tmp_path: Path) -> None:
    """End to end, `kinoforge list` must see what the pod just loaded.

    Bug caught: dropping the `set_lora_stack` response on the floor. The
    CLI renders its LoRA section straight off the row's
    ``lora_inventory`` (`cli/_commands.py`), and `warm_reuse/matcher.py`
    plans swaps from the same field — so a pod that just accepted a
    2-entry stack would report an empty inventory to both. A unit test
    with a ledger spy cannot catch a row written under the wrong
    instance id; this one reads the row back through the real Ledger.
    """
    engine = _LoraSpyEngine()
    store = LocalArtifactStore(tmp_path)

    with deploy_session(
        _cfg(["hf:org/repo:a.safetensors", "hf:org/repo:b.safetensors"]),
        store=store,
        engine=engine,
        provider=LocalProvider(),
        creds=_NullCreds(),
        run_id="r",
    ) as session:
        assert session.instance is not None
        pod_id = session.instance.id

    entry = Ledger(store=store).read(pod_id)
    assert entry is not None, "deploy_session must have recorded a row for this pod"
    inventory = entry["lora_inventory"]
    assert len(inventory) == 2, (
        "the row must carry one entry per LoRA the pod reported holding"
    )
    assert [e["filename"] for e in inventory] == ["0.safetensors", "1.safetensors"]
    assert entry["loras_dir_free_bytes"] == 12345
    assert isinstance(entry["loras_dir_free_bytes_observed_at_local"], str)
    # Every ref the pod reported back is a redaction PLACEHOLDER on disk,
    # not the raw ref. That is the canonical ledger shape
    # (``Ledger._persist`` runs ``redact_json`` over the whole payload) and
    # it only happens if ``_register_observed_lora_refs`` ran BEFORE the
    # touch. Asserting it here pins that ordering end to end: reverse the
    # two and these rows persist unredacted vault refs to disk.
    assert all(e["ref"].startswith("<lora:ref:") for e in inventory), (
        f"pod-reported refs must be registered before the write; got {inventory}"
    )


def test_a_run_without_loras_leaves_the_inventory_field_alone(
    tmp_path: Path,
) -> None:
    """No LoRAs must not mean "wipe whatever the row already knew".

    Bug caught: writing an unconditional empty ``lora_inventory`` on
    every session. Attaching to a warm pod that genuinely holds two
    adapters with a no-LoRA config would then erase the matcher's only
    record of them.
    """
    engine = _LoraSpyEngine()
    store = LocalArtifactStore(tmp_path)

    with deploy_session(
        _cfg([]),
        store=store,
        engine=engine,
        provider=LocalProvider(),
        creds=_NullCreds(),
        run_id="r",
    ) as session:
        assert session.instance is not None
        pod_id = session.instance.id

    entry = Ledger(store=store).read(pod_id)
    assert entry is not None
    assert "lora_inventory" not in entry


def test_hosted_engine_with_loras_still_runs(tmp_path: Path) -> None:
    """A backend with no LoRA surface is a no-op, not a crash.

    Bug caught: an unguarded ``backend.set_lora_stack(...)`` turning
    every hosted-engine config that carries ``loras:`` into an
    AttributeError at session entry.
    """
    engine = _HostedPlainEngine()
    store = LocalArtifactStore(tmp_path)

    with deploy_session(
        _cfg(["hf:org/repo:a.safetensors"], hosted=True),
        store=store,
        engine=engine,
        creds=_NullCreds(),
        run_id="r",
    ) as session:
        assert session.instance is None
        session.pool.submit(GenerationJob(spec={}, params={}, segments=[])).result(
            timeout=5.0
        )


def test_hosted_lora_capable_backend_without_a_pod_does_not_post(
    tmp_path: Path,
) -> None:
    """No instance means no pod to address — skip, do not invent one.

    Bug caught: passing ``pod_id=None`` onto the wire for a hosted
    engine whose backend happens to expose ``set_lora_stack``.
    """
    engine = _HostedLoraSpyEngine()
    store = LocalArtifactStore(tmp_path)

    with deploy_session(
        _cfg(["hf:org/repo:a.safetensors"], hosted=True),
        store=store,
        engine=engine,
        creds=_NullCreds(),
        run_id="r",
    ) as session:
        assert session.instance is None

    assert engine.backends[-1].set_stack_calls == []


# ---------------------------------------------------------------------------
# `kinoforge grid` control cells — the path where a stale stack bites hardest
# ---------------------------------------------------------------------------

_LAUNCHER_PREFIX = ["pixi", "run", "kinoforge"]


def _swap_cell(tmp_path: Path, *, stack: list[LoraStackEntry]) -> _ResolvedCell:
    """Build one swap-mode grid cell carrying ``stack``.

    Args:
        tmp_path: Directory the cell's rendered config is written into.
        stack: The cell's ``lora_swap_stack``; empty means a no-LoRA
            control cell.

    Returns:
        A minimal :class:`_ResolvedCell` accepted by
        ``_build_swap_generate_cmd``.
    """
    cfg_path = tmp_path / "cell.yaml"
    cfg_path.write_text("model: fake\nprompt: hi\n")
    return _ResolvedCell(
        idx=1,
        caption="control",
        cfg_path=cfg_path,
        effective_cfg=SimpleNamespace(prompt="hi", mode="t2v"),
        mp4_path=None,
        is_lora_swap=True,
        lora_swap_stack=list(stack),
    )


def test_a_grid_control_cell_clears_the_previous_cells_lora(tmp_path: Path) -> None:
    """A swap grid's no-LoRA control cell must render LoRA-free.

    Drives the REAL chain a grid cell takes — ``_build_swap_generate_cmd``
    renders the argv, the production CLI parser reads ``--loras`` off it,
    ``parse_loras_heredoc`` turns the empty body into ``[]``, that lands on
    the ``EphemeralSession`` exactly as ``_cmd_generate`` stashes it, and
    ``deploy_session`` applies it. Nothing here restates the fix; every hop
    is production code.

    Bug caught: cells 2..N of a ``lora_swap`` grid share ONE warm pod
    (``executor.py`` passes ``--attach-pod`` and never ``--no-reuse``), and
    ``executor.py``'s ``_stack_to_loras_heredoc`` emits ``""`` to mean
    "clear". If the empty stack is a no-op the control cell renders with
    the PREVIOUS cell's adapter still loaded, so a strength sweep's
    baseline is silently the wrong picture — and every cell downstream is
    compared against it.
    """
    cmd = _build_swap_generate_cmd(
        _swap_cell(tmp_path, stack=[]),
        grid_id="grid_clear",
        output_dir=tmp_path / "out",
        attach_pod_id="pod-warm",
        emit_provision_record=None,
    )
    assert cmd[:3] == _LAUNCHER_PREFIX, (
        f"cell argv must start with the pixi launcher prefix, got {cmd[:3]}"
    )
    args = _build_parser().parse_args(cmd[3:])
    assert args.loras == "", (
        f"the control cell must pass an EMPTY --loras body, got {args.loras!r}"
    )

    cli_loras = parse_loras_heredoc(args.loras)
    assert cli_loras == [], "an empty heredoc parses to the explicit empty stack"

    engine = _LoraSpyEngine()
    store = LocalArtifactStore(tmp_path)

    with EphemeralSession(enabled=False) as session:
        session.cli_loras = cli_loras
        with deploy_session(
            _cfg(["hf:org/repo:previous-cell.safetensors"]),
            store=store,
            engine=engine,
            provider=LocalProvider(),
            creds=_NullCreds(),
            run_id="r",
        ) as gen_session:
            assert gen_session.instance is not None
            pod_id = gen_session.instance.id

    backend = engine.backends[-1]
    assert len(backend.set_stack_calls) == 1, (
        "the control cell must issue exactly one clearing set_lora_stack; got "
        f"{len(backend.set_stack_calls)}"
    )
    called_pod, active_stack, specs = backend.set_stack_calls[0]
    assert called_pod == pod_id
    assert active_stack == [], (
        f"the pod must be told to hold nothing; got {[e.ref for e in active_stack]}"
    )
    assert specs == {}

    entry = Ledger(store=store).read(pod_id)
    assert entry is not None
    assert entry["lora_inventory"] == [], (
        "a cleared pod's row must say it holds nothing, or `kinoforge list` "
        f"and the warm-reuse matcher keep planning against ghosts; got {entry}"
    )
