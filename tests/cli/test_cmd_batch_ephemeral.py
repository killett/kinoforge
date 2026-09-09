"""U23 — ``_cmd_batch`` must reserve its ephemeral launch row before it books.

A branch merged at ``d5dc0433`` made ``generate``, ``upscale`` and
``interpolate`` write a durable ephemeral-index row (Spec A2,
``_ephemeral_launch_row_reserve``) *before* ``create_instance`` — so a
crashed run leaves a pod that can still be named and destroyed. It did not
reach ``batch``.

``batch`` is the longest-running command there is: one shared pod services an
entire manifest, so the pre-fix crash window ran for the whole batch, not
just a cold boot. Under ``--ephemeral`` the ledger write is suppressed and
lives only in ``session.in_memory_ledger`` (spec STRICT_POLICY), so for the
whole run the pod existed in NO on-disk state file at all.

Every assertion below reads the ephemeral index through a FRESH
``SessionContext`` built from the same ``state_dir`` — the exact on-disk path
a separate ``kinoforge list`` process (or the sweeper) would use. No
assertion is satisfiable by an in-memory object the run under test happens
to hold.

The ordering test goes one step further than the merged ``generate`` /
``upscale`` / ``interpolate`` suite: rather than stubbing the orchestrator
entry point, it substitutes ``LocalProvider.create_instance`` itself and
reads the index from inside that call — the boundary money is actually
committed across for the real ``batch_generate`` -> ``deploy_session`` ->
``provider.create_instance`` chain, not a stand-in for it.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

import kinoforge.cli  # noqa: F401 — registers CLI re-exports
import kinoforge.engines.fake  # noqa: F401 — self-registers engine "fake"
import kinoforge.providers.local  # noqa: F401 — self-registers provider "local"
import kinoforge.sources.http  # noqa: F401 — registers the https:// source
from kinoforge.cli._commands import _cmd_batch
from kinoforge.cli.context import SessionContext
from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.errors import AuthError
from kinoforge.core.interfaces import Instance
from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex, EphemeralIndexRow
from kinoforge.providers.local import LocalProvider

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

_MANIFEST_YAML = """\
- prompt: row-1
  mode: t2v
- prompt: row-2
  mode: t2v
"""


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


def _write_manifest(tmp_path: Path, name: str = "manifest.yaml") -> Path:
    """Write the shared two-row manifest and return its path."""
    p = tmp_path / name
    p.write_text(_MANIFEST_YAML)
    return p


def _ctx_for(cfg_path: Path, state_dir: Path) -> SessionContext:
    """Build a SessionContext the way one ``kinoforge`` process does.

    A FRESH context per call is deliberate: it resolves the store exactly as
    a separate process would, so nothing below can pass on in-memory state.
    """
    return SessionContext.from_args(state_dir=state_dir, cfg_path=cfg_path)


def _index_rows(cfg_path: Path, state_dir: Path) -> list[EphemeralIndexRow]:
    """Read ``ephemeral-index.json`` off disk, as another process would."""
    return EphemeralIndex(store=_ctx_for(cfg_path, state_dir).store()).rows()


def _make_args(
    *,
    manifest_path: Path,
    batch_id: str,
    no_reuse: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=manifest_path,
        batch_id=batch_id,
        concurrent=1,
        env_file=None,
        instance_id=None,
        force_attach=False,
        no_reuse=no_reuse,
        output_dir=None,
        no_output_dir=True,
        stream_format="none",
    )


# ---------------------------------------------------------------------------
# Behaviour 1 — the row exists before the pod exists.
# ---------------------------------------------------------------------------


def test_ephemeral_batch_reserves_a_row_before_the_pod_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug caught: _cmd_batch never reserved a launch row, so for the whole
    batch — the longest-running command there is — the pod existed in no
    state file and a crash stranded a billing GPU nothing could name."""
    cfg_path, state_dir = _write_cfg(tmp_path)
    manifest_path = _write_manifest(tmp_path)

    seen_at_create: list[list[EphemeralIndexRow]] = []
    original_create = LocalProvider.create_instance

    def spying_create(self: LocalProvider, spec: Any) -> Instance:
        # The assertion is taken from INSIDE create_instance, reading
        # through a fresh SessionContext against the same state dir — the
        # same on-disk path a separate `kinoforge list` process would use.
        # An implementation that reserves AFTER batch_generate returns
        # (or never at all) fails here even though the end state would
        # look identical afterwards.
        seen_at_create.append(_index_rows(cfg_path, state_dir))
        return original_create(self, spec)

    monkeypatch.setattr(LocalProvider, "create_instance", spying_create)

    ctx = _ctx_for(cfg_path, state_dir)
    with EphemeralSession(enabled=True) as session:
        rc = _cmd_batch(
            _make_args(manifest_path=manifest_path, batch_id="b-order"), ctx
        )
        expected_id = session.resource_name("b-order", "local")

    assert rc == 0
    assert len(seen_at_create) == 1, "create_instance must run exactly once"
    rows_at_create = seen_at_create[0]
    assert rows_at_create, (
        "ephemeral-index.json held no rows while create_instance was in "
        "flight — the pod existed in no state file for the whole batch, "
        "and no endpoint existed for a monitor to poll"
    )
    row = rows_at_create[0]
    assert row.id == expected_id, (
        "the pre-create row must be keyed by the name the provider will "
        f"give the resource; got {row.id!r}"
    )
    assert row.provider == "local"
    assert row.endpoints == {}, (
        "no endpoint is knowable before create_instance returns; a "
        "non-empty dict here means the row was written too late to matter"
    )


# ---------------------------------------------------------------------------
# Behaviour 2 — the gate is strict-policy, not "a session is active".
# ---------------------------------------------------------------------------


def test_ordinary_batch_reserves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ledger already covers the non-ephemeral path; reserving there
    would put an endpoint-less row in front of the warm matcher."""
    cfg_path, state_dir = _write_cfg(tmp_path)
    manifest_path = _write_manifest(tmp_path)

    create_calls: list[bool] = []
    original_create = LocalProvider.create_instance

    def counting_create(self: LocalProvider, spec: Any) -> Instance:
        create_calls.append(True)
        return original_create(self, spec)

    monkeypatch.setattr(LocalProvider, "create_instance", counting_create)

    ctx = _ctx_for(cfg_path, state_dir)
    # `_main.py` wraps EVERY dispatch in a session — enabled=False is the
    # ordinary (non `--ephemeral`) shape, not "no session at all".
    with EphemeralSession(enabled=False):
        rc = _cmd_batch(
            _make_args(manifest_path=manifest_path, batch_id="b-ordinary"), ctx
        )

    assert rc == 0
    assert create_calls == [True], "the pod must still have been created"
    rows = _index_rows(cfg_path, state_dir)
    assert rows == [], (
        f"an ordinary batch reserved {len(rows)} ephemeral-index row(s); "
        "the gate must be _ephemeral_strict_session(), not "
        "EphemeralSession.current() is not None"
    )


# ---------------------------------------------------------------------------
# Behaviour 3 — ruling C1: a raise keeps the row.
# ---------------------------------------------------------------------------


def test_batch_row_survives_a_create_that_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling C1: a failed create keeps its row for the classifier to age out."""
    cfg_path, state_dir = _write_cfg(tmp_path)
    manifest_path = _write_manifest(tmp_path)

    def raising_create(self: LocalProvider, spec: Any) -> Instance:
        # AuthError, not CapacityError: CapacityError triggers the
        # capacity-wait retry loop in orchestrator._create_with_capacity_retry
        # (real sleeps), which is irrelevant to what this test checks and
        # would just make it slow / flaky. Any exception is C1's premise;
        # this is a realistic one that propagates on the first attempt.
        raise AuthError("bad credentials for test")

    monkeypatch.setattr(LocalProvider, "create_instance", raising_create)

    ctx = _ctx_for(cfg_path, state_dir)
    with EphemeralSession(enabled=True) as session:
        rc = _cmd_batch(
            _make_args(manifest_path=manifest_path, batch_id="b-raise"), ctx
        )
        expected_id = session.resource_name("b-raise", "local")

    assert rc == 1
    rows = _index_rows(cfg_path, state_dir)
    assert len(rows) == 1, (
        f"expected the launch row to survive the raise; found {len(rows)} "
        "rows — either it was never written, or it was wrongly released"
    )
    assert rows[0].id == expected_id


# ---------------------------------------------------------------------------
# Behaviour 4 — settled the same way `generate` settles it: released only
# on a confirmed destroy.
# ---------------------------------------------------------------------------


def test_ephemeral_batch_no_reuse_releases_row_on_confirmed_destroy(
    tmp_path: Path,
) -> None:
    """Bug caught: an implementation that reserves the row but never
    settles it would leave ephemeral-index.json still naming a pod
    ``--no-reuse`` already confirmed destroyed — a phantom entry the
    classifier can never clear because nothing living backs it."""
    cfg_path, state_dir = _write_cfg(tmp_path)
    manifest_path = _write_manifest(tmp_path)

    ctx = _ctx_for(cfg_path, state_dir)
    with EphemeralSession(enabled=True):
        rc = _cmd_batch(
            _make_args(
                manifest_path=manifest_path, batch_id="b-noreuse", no_reuse=True
            ),
            ctx,
        )

    assert rc == 0
    rows = _index_rows(cfg_path, state_dir)
    assert rows == [], (
        f"expected the launch row released after a confirmed --no-reuse "
        f"destroy; found {len(rows)} row(s) still naming a pod that is gone"
    )


# ---------------------------------------------------------------------------
# Behaviour 5 — several launches do not collide on a reserved name.
# ---------------------------------------------------------------------------


def test_two_ephemeral_batches_reserve_distinct_names(tmp_path: Path) -> None:
    """Bug caught: if EphemeralSession.resource_name memoised per-session
    rather than per-run-id, or _cmd_batch cached one reserved id instead of
    deriving it fresh from each batch_id, two batches sharing a process
    would reserve — and then both try to name their pod with — the exact
    same token, colliding on one row for two different pods."""
    cfg_path, state_dir = _write_cfg(tmp_path)
    manifest_a = _write_manifest(tmp_path, name="manifest-a.yaml")
    manifest_b = _write_manifest(tmp_path, name="manifest-b.yaml")

    ctx = _ctx_for(cfg_path, state_dir)
    with EphemeralSession(enabled=True) as session:
        rc_a = _cmd_batch(_make_args(manifest_path=manifest_a, batch_id="b-one"), ctx)
        rc_b = _cmd_batch(_make_args(manifest_path=manifest_b, batch_id="b-two"), ctx)
        id_a = session.resource_name("b-one", "local")
        id_b = session.resource_name("b-two", "local")

    assert rc_a == 0
    assert rc_b == 0
    assert id_a != id_b, "two distinct batch_ids minted the same reserved name"
