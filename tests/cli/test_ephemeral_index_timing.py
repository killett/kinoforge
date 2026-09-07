"""The ephemeral index row must name a launch from the moment it starts billing.

An ``--ephemeral`` run deliberately writes no durable ledger row — that is the
point of the flag. Its ONLY durable trace is a row in
``ephemeral-index.json``, and until 2026-09-06 that row was written *after*
the orchestrator returned, stamped with completion time. A run launched at
02:01:23 produced a row stamped 02:02:41. For that whole window nothing
anywhere named the pod, so a Ctrl-C, an OOM, or a session death stranded a
billing GPU that no kinoforge command and no state file could identify — and a
monitor could not poll the pod's ``/util`` endpoint, because the endpoint had
not been written down yet.

Every assertion below reads the index through a FRESH ``SessionContext`` built
from the same ``state_dir``, i.e. the exact on-disk path a separate
``kinoforge list`` process (or the sweeper) would use. No assertion can be
satisfied by an in-memory object the run under test happens to hold.

The orchestrator's ``generate`` is substituted with a stub that reads the
index at the instant it is entered. That entry is the boundary money is
committed across: ``provider.create_instance`` runs strictly inside it, so a
row absent at the stub's entry cannot be present inside ``create_instance``.
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

import kinoforge.cli  # noqa: F401 — registers the `generate` re-export
import kinoforge.engines.fake  # noqa: F401 — self-registers engine "fake"
import kinoforge.providers.local  # noqa: F401 — self-registers provider "local"
import kinoforge.sources.http  # noqa: F401 — registers the https:// source
from kinoforge.cli._commands import _cmd_generate
from kinoforge.cli.context import SessionContext
from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.interfaces import Artifact, Instance
from kinoforge.core.warm_reuse.ephemeral_index import (
    EphemeralIndex,
    EphemeralIndexRow,
)

_CFG_YAML = """\
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

_RUN_ID = "run-cold-1"
_POD_ID = "local-cold-1"
_POD_ENDPOINTS = {"8188": "http://local-cold-1.invalid"}


def _write_cfg(tmp_path: Path) -> tuple[Path, Path]:
    """Materialise the cfg + state dir one CLI invocation needs.

    Returns:
        ``(cfg_path, state_dir)``.
    """
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(_CFG_YAML)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return cfg_path, state_dir


def _ctx_for(cfg_path: Path, state_dir: Path) -> SessionContext:
    """Build a SessionContext the way one ``kinoforge`` process does.

    A FRESH context per call is deliberate: it resolves the store exactly as a
    separate process would, so nothing below can pass on in-memory state.
    """
    return SessionContext.from_args(state_dir=state_dir, cfg_path=cfg_path)


def _index_rows(cfg_path: Path, state_dir: Path) -> list[EphemeralIndexRow]:
    """Read ``ephemeral-index.json`` off disk, as another process would."""
    return EphemeralIndex(store=_ctx_for(cfg_path, state_dir).store()).rows()


def _make_args(*, run_id: str = _RUN_ID, no_reuse: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        config="<unused>",
        prompt="x",
        mode="t2v",
        run_id=run_id,
        output_dir=None,
        no_output_dir=True,
        instance_id=None,
        force_attach=False,
        no_reuse=no_reuse,
        skip_preflight=True,
        dry_run_swap=False,
        env_file=None,
        loras=None,
        attach_pod=None,
        emit_provision_record=None,
        diagnostic_mode=False,
    )


def _cold_instance() -> Instance:
    """The instance a cold create returns — id and endpoints only knowable after."""
    return Instance(
        id=_POD_ID,
        provider="local",
        status="ready",
        created_at=0.0,
        tags={},
        cost_rate_usd_per_hr=0.0,
        endpoints=dict(_POD_ENDPOINTS),
    )


class _CreateWindow:
    """Stub orchestrator standing in for the window money is committed across.

    ``read_at_entry`` is the index as a separate process would have seen it at
    the instant ``create_instance`` was about to be called. ``entered_at`` is
    when that happened, so a test can tell a launch-time stamp from a
    completion-time one.
    """

    def __init__(
        self,
        cfg_path: Path,
        state_dir: Path,
        *,
        returns: Instance | None,
        raises: BaseException | None = None,
        dwell_s: float = 0.05,
    ) -> None:
        self._cfg_path = cfg_path
        self._state_dir = state_dir
        self._returns = returns
        self._raises = raises
        self._dwell_s = dwell_s
        self.calls = 0
        self.entered_at: datetime | None = None
        self.read_at_entry: list[EphemeralIndexRow] = []

    def __call__(self, cfg: Any, request: Any, **kw: Any) -> tuple[Artifact, Any]:
        del cfg, request, kw
        self.calls += 1
        self.entered_at = datetime.now()
        self.read_at_entry = _index_rows(self._cfg_path, self._state_dir)
        # A real create is multi-minute. 50 ms is enough that an ISO stamp
        # taken after this returns is unambiguously later than one taken
        # before it, at microsecond resolution.
        time.sleep(self._dwell_s)
        if self._raises is not None:
            raise self._raises
        return (Artifact(uri="file:///out.mp4", sha256="ab", size=1), self._returns)


def _install(monkeypatch: pytest.MonkeyPatch, window: _CreateWindow) -> _CreateWindow:
    """Swap the orchestrator entry point ``_cmd_generate`` resolves."""
    monkeypatch.setattr("kinoforge.cli.generate", window, raising=False)
    monkeypatch.setattr("kinoforge.cli._commands.generate", window)
    return window


def _drive(
    cfg_path: Path,
    state_dir: Path,
    window: _CreateWindow,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ephemeral: bool = True,
    no_reuse: bool = False,
) -> int:
    """Run one ``kinoforge generate`` invocation against *window*."""
    _install(monkeypatch, window)
    ctx = _ctx_for(cfg_path, state_dir)
    if not ephemeral:
        return _cmd_generate(_make_args(no_reuse=no_reuse), ctx)
    with EphemeralSession(enabled=True):
        return _cmd_generate(_make_args(no_reuse=no_reuse), ctx)


# ---------------------------------------------------------------------------
# Behaviour 1 — the row exists before the create commits money.
# ---------------------------------------------------------------------------


def test_ephemeral_row_exists_before_create_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug caught: the row landed at completion, so a kill mid-run left a
    billing Modal app that no state file and no kinoforge command could
    name. Reading the index during a live run showed ``{"rows": []}``.

    An implementation that writes the row only after the orchestrator returns
    fails here even though the end state looks identical — the ordering is the
    fix, so the assertion is taken from inside the window.
    """
    cfg_path, state_dir = _write_cfg(tmp_path)
    window = _CreateWindow(cfg_path, state_dir, returns=_cold_instance())

    rc = _drive(cfg_path, state_dir, window, monkeypatch)

    assert rc == 0
    assert window.calls == 1
    assert window.read_at_entry, (
        "ephemeral-index.json held no rows while the create was in flight — "
        "the run had committed money with nothing durable naming what it was "
        "about to book, and no endpoint for a monitor to poll"
    )
    row = window.read_at_entry[0]
    assert row.id == _RUN_ID, (
        "the pre-create row must be keyed by the client-side run id, which is "
        f"what the provider names the resource with; got {row.id!r}"
    )
    cfg = _ctx_for(cfg_path, state_dir).cfg
    assert cfg is not None
    assert row.kinoforge_key == cfg.capability_key().derive()[:12]
    assert row.provider == "local"
    assert row.endpoints == {}, (
        "no endpoint is knowable before create_instance returns; a non-empty "
        "map here would be invented"
    )


def test_no_row_is_written_outside_an_ephemeral_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug caught: hoisting the write above the EphemeralSession gate, so every
    ordinary (ledger-backed) run starts leaving rows in the ephemeral index —
    rows the sweeper then probe-and-reaps alongside the real ledger row."""
    cfg_path, state_dir = _write_cfg(tmp_path)
    window = _CreateWindow(cfg_path, state_dir, returns=_cold_instance())

    rc = _drive(cfg_path, state_dir, window, monkeypatch, ephemeral=False)

    assert rc == 0
    assert window.read_at_entry == []
    assert _index_rows(cfg_path, state_dir) == []


def test_no_row_is_written_when_the_run_attaches_to_a_warm_pod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug caught: reserving unconditionally. A warm-attach run creates
    nothing, so a run-id row would name no resource at all — a permanent
    phantom the matcher would try to attach to and the sweeper would probe
    forever."""
    cfg_path, state_dir = _write_cfg(tmp_path)
    warm = Instance(
        id="warm-pod",
        provider="local",
        status="ready",
        created_at=0.0,
        tags={},
        cost_rate_usd_per_hr=0.0,
        endpoints={"8188": "http://warm-pod.invalid"},
    )
    cfg = _ctx_for(cfg_path, state_dir).cfg
    assert cfg is not None
    seeded = EphemeralIndexRow(
        id=warm.id,
        warm_attach_key="wak-irrelevant",
        kinoforge_key=cfg.capability_key().derive()[:12],
        endpoints=dict(warm.endpoints),
        provider="local",
        created_at_local=datetime.now().isoformat(),
    )
    window = _CreateWindow(cfg_path, state_dir, returns=warm)
    _install(monkeypatch, window)
    monkeypatch.setattr(
        "kinoforge.cli._commands._scan_warm_candidates",
        lambda ctx, cfg: (warm, _StubReport()),
    )

    ctx = _ctx_for(cfg_path, state_dir)
    with EphemeralSession(enabled=True):
        EphemeralIndex(store=ctx.store()).add(seeded)
        rc = _cmd_generate(_make_args(), ctx)

    assert rc == 0
    assert [r.id for r in window.read_at_entry] == ["warm-pod"], (
        "a warm-attach run must not reserve a launch row for a create that "
        "never happens"
    )
    assert [r.id for r in _index_rows(cfg_path, state_dir)] == ["warm-pod"]


class _StubReport:
    """Minimal stand-in for ``_ScanReport`` — only ``summarize`` is called."""

    def summarize(self) -> str:
        return "stub scan report"


# ---------------------------------------------------------------------------
# Behaviour 2 — the stamp is launch time.
# ---------------------------------------------------------------------------


def test_ephemeral_row_is_stamped_with_launch_time_not_completion_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug caught: a run launched 02:01:23 produced a row stamped 02:02:41,
    so any age-based reaping under-counts the pod's real lifetime by the
    entire boot window — a pod already an hour old reads as newly born.

    The expected bound is taken from the clock, not from the implementation:
    the stamp must fall between the moment before the CLI was called and the
    moment the create window was entered, and must NOT fall after it.
    """
    cfg_path, state_dir = _write_cfg(tmp_path)
    window = _CreateWindow(cfg_path, state_dir, returns=_cold_instance())

    before = datetime.now()
    rc = _drive(cfg_path, state_dir, window, monkeypatch)
    after = datetime.now()

    assert rc == 0
    assert window.entered_at is not None
    rows = _index_rows(cfg_path, state_dir)
    assert len(rows) == 1
    stamped = datetime.fromisoformat(rows[0].created_at_local)
    assert before <= stamped <= window.entered_at, (
        f"created_at_local={stamped.isoformat()} is not launch time: the "
        f"launch happened at or before {window.entered_at.isoformat()} and "
        f"the run only finished at {after.isoformat()}"
    )
    assert rows[0].created_at_local == window.read_at_entry[0].created_at_local, (
        "the surviving row must carry the stamp the pre-create row was written "
        "with, verbatim — re-stamping at completion is the bug"
    )


# ---------------------------------------------------------------------------
# Behaviour 3 — one pod, one row.
# ---------------------------------------------------------------------------


def test_ephemeral_row_is_updated_not_duplicated_when_endpoints_arrive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One pod, one row.

    Bug caught: writing the pre-create row under the run id and the post-create
    row under the pod id without collapsing the first, which leaves two rows
    for one pod — the matcher would try to attach to a run id that names
    nothing, and the sweeper would probe a phantom on every sweep.
    """
    cfg_path, state_dir = _write_cfg(tmp_path)
    window = _CreateWindow(cfg_path, state_dir, returns=_cold_instance())

    rc = _drive(cfg_path, state_dir, window, monkeypatch)

    assert rc == 0
    rows = _index_rows(cfg_path, state_dir)
    assert [r.id for r in rows] == [_POD_ID], (
        f"expected exactly one row, keyed by the pod id; got {[r.id for r in rows]}"
    )
    assert rows[0].endpoints == _POD_ENDPOINTS, (
        "the row must be updated with the endpoints the create returned, or a "
        "monitor still has nothing to poll"
    )


def test_the_launch_row_is_dropped_when_no_reuse_destroys_the_pod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--no-reuse`` tears the pod down, so its launch row must not survive.

    Bug caught: reserving the row and never releasing it on the one-shot path,
    which leaves a row for a pod that no longer exists — the next
    ``--ephemeral`` run's matcher picks it, probes a dead pod, and the sweeper
    carries a 404 phantom until it ages out.

    The row must still be present DURING the run: that window is exactly what
    a one-shot smoke needs protecting.
    """
    cfg_path, state_dir = _write_cfg(tmp_path)
    window = _CreateWindow(cfg_path, state_dir, returns=_cold_instance())

    rc = _drive(cfg_path, state_dir, window, monkeypatch, no_reuse=True)

    assert rc == 0
    assert [r.id for r in window.read_at_entry] == [_RUN_ID], (
        "a one-shot run is the case most likely to be interrupted; it must "
        "still be nameable while it is in flight"
    )
    assert _index_rows(cfg_path, state_dir) == []


# ---------------------------------------------------------------------------
# Behaviour 4 — ruling C1: a raise keeps the row.
# ---------------------------------------------------------------------------


def test_ephemeral_row_survives_a_create_that_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed run keeps its row for the classifier to age out (ruling C1).

    Bug caught: cleaning the row up in an ``except``. A raise is not evidence
    the provider booked nothing — a timeout, an OOM, or a Ctrl-C lands here
    while the provider's API server goes on creating the resource — so
    deleting it strands a billing pod with no durable name anywhere, which is
    the leak this whole change closes, reintroduced on the failure path.
    """
    cfg_path, state_dir = _write_cfg(tmp_path)
    boom = RuntimeError("provider API timed out mid-create")
    window = _CreateWindow(cfg_path, state_dir, returns=None, raises=boom)

    with pytest.raises(RuntimeError, match="timed out mid-create"):
        _drive(cfg_path, state_dir, window, monkeypatch)

    rows = _index_rows(cfg_path, state_dir)
    assert [r.id for r in rows] == [_RUN_ID], (
        f"expected the launch row to survive the raise, got {[r.id for r in rows]}"
    )
    assert rows[0].endpoints == {}
