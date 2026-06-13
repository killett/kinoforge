"""B3 Task e — _cmd_batch scan dispatch + precedence."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

import kinoforge.cli  # noqa: F401
import kinoforge.engines.fake  # noqa: F401
import kinoforge.providers.local  # noqa: F401
import kinoforge.sources.http  # noqa: F401
from kinoforge.cli._commands import _cmd_batch, _ScanReport
from kinoforge.cli.context import SessionContext
from kinoforge.core.config import load_config
from kinoforge.core.interfaces import Instance

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


def _make_ctx(tmp_path: Path) -> tuple[SessionContext, Path]:
    p = tmp_path / "cfg.yaml"
    p.write_text(_CFG_YAML)
    cfg = load_config(p)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(_MANIFEST_YAML)
    return (
        SessionContext(state_dir=state_dir, cfg=cfg, sidecar=None),
        manifest,
    )


def _make_args(
    *,
    manifest_path: Path,
    instance_id: str | None = None,
    no_reuse: bool = False,
    force_attach: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        manifest=manifest_path,
        batch_id="b-test",
        concurrent=1,
        env_file=None,
        instance_id=instance_id,
        force_attach=force_attach,
        no_reuse=no_reuse,
        output_dir=None,
        no_output_dir=True,
        stream_format="none",
    )


def _stub_batch(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    from kinoforge.core.batch_models import BatchOutcome, BatchResult

    def _bg(cfg: Any, manifest: Any, **kw: Any) -> BatchResult:
        captured.update(kw)
        return BatchResult(
            batch_id="b-test",
            started_at="2026-06-13T00:00:00",
            finished_at="2026-06-13T00:00:01",
            outcomes=[
                BatchOutcome(run_id="0", status="ok", duration_s=0.1, uri="x://1"),
                BatchOutcome(run_id="1", status="ok", duration_s=0.1, uri="x://2"),
            ],
        )

    monkeypatch.setattr("kinoforge.core.batch.batch_generate", _bg)


def _fake_instance(iid: str) -> Instance:
    return Instance(
        id=iid,
        provider="local",
        tags={},
        created_at=0.0,
        cost_rate_usd_per_hr=0.0,
        status="ready",
        endpoints={},
    )


# ---------------------------------------------------------------------------
# Default → scan dispatched
# ---------------------------------------------------------------------------


def test_batch_calls_scan_when_no_explicit_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: batch skipping scan would force every fresh shell to cold-create."""
    scan_called: list[bool] = []

    def spy_scan(ctx: Any, cfg: Any, **kw: Any) -> tuple[Any, _ScanReport]:
        scan_called.append(True)
        return (None, _ScanReport())

    monkeypatch.setattr("kinoforge.cli._commands._scan_warm_candidates", spy_scan)
    captured: dict[str, Any] = {}
    _stub_batch(monkeypatch, captured)

    ctx, manifest = _make_ctx(tmp_path)
    rc = _cmd_batch(_make_args(manifest_path=manifest), ctx)
    assert rc == 0
    assert scan_called == [True]


def test_batch_no_reuse_skips_scan_and_threads_single_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: per-row destroy would defeat cost-amortization batch is for;
    skipping single=True would leak the pod after the batch."""
    scan_called: list[bool] = []

    def spy_scan(*a: Any, **kw: Any) -> tuple[Any, _ScanReport]:
        scan_called.append(True)
        return (None, _ScanReport())

    monkeypatch.setattr("kinoforge.cli._commands._scan_warm_candidates", spy_scan)
    captured: dict[str, Any] = {}
    _stub_batch(monkeypatch, captured)
    ctx, manifest = _make_ctx(tmp_path)
    rc = _cmd_batch(_make_args(manifest_path=manifest, no_reuse=True), ctx)
    assert rc == 0
    assert scan_called == []
    assert captured["single"] is True


def test_batch_passes_instance_kwarg_from_scan_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: not threading scan's instance into batch_generate would re-cold-create."""
    hit = _fake_instance("warm-pod")
    monkeypatch.setattr(
        "kinoforge.cli._commands._scan_warm_candidates",
        lambda *a, **kw: (hit, _ScanReport(attached="warm-pod")),
    )
    captured: dict[str, Any] = {}
    _stub_batch(monkeypatch, captured)
    ctx, manifest = _make_ctx(tmp_path)
    rc = _cmd_batch(_make_args(manifest_path=manifest), ctx)
    assert rc == 0
    assert captured["instance"] is hit


def test_batch_no_reuse_with_force_attach_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bug: not enforcing mutex would let operators stumble into incoherent state."""
    ctx, manifest = _make_ctx(tmp_path)
    rc = _cmd_batch(
        _make_args(manifest_path=manifest, no_reuse=True, force_attach=True), ctx
    )
    assert rc == 2
    assert "mutually exclusive" in capsys.readouterr().err
