"""`kinoforge generate --attach-pod` + `--emit-provision-record` CLI surface.

`--attach-pod POD_ID`: skip provision, attach to a ledger-recorded
running pod whose ``warm_attach_key`` matches the cfg. Distinct from
``--instance-id``, which uses full ``CapabilityKey`` + the matcher (and
would reject a different LoRA stack).

`--emit-provision-record PATH`: on cold-boot success, write
``{pod_id, endpoint_url, provider, warm_attach_key, provision_ts}`` so
``kinoforge grid`` swap-mode (or any operator script) can hand the pod
off to a follow-up ``--attach-pod`` call.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import kinoforge.cli  # noqa: F401 — registers `generate` re-export
import kinoforge.engines.fake  # noqa: F401
import kinoforge.providers.local  # noqa: F401
import kinoforge.sources.http  # noqa: F401
from kinoforge.cli._commands import _cmd_generate
from kinoforge.cli.context import SessionContext
from kinoforge.core.config import load_config
from kinoforge.core.interfaces import Instance, WarmAttachKey

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


def _make_ctx(tmp_path: Path) -> SessionContext:
    p = tmp_path / "cfg.yaml"
    p.write_text(_CFG_YAML)
    cfg = load_config(p)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return SessionContext(state_dir=state_dir, cfg=cfg, sidecar=None)


def _make_args(
    *,
    attach_pod: str | None = None,
    emit_provision_record: Path | None = None,
    no_reuse: bool = False,
    instance_id: str | None = None,
    force_attach: bool = False,
    loras: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        prompt="x",
        mode="t2v",
        run_id="r-1",
        instance_id=instance_id,
        force_attach=force_attach,
        no_reuse=no_reuse,
        output_dir=None,
        no_output_dir=True,
        skip_preflight=True,
        attach_pod=attach_pod,
        emit_provision_record=emit_provision_record,
        loras=loras,
        dry_run_swap=False,
    )


def _fake_instance(
    iid: str = "pod-1",
    *,
    status: str = "ready",
    endpoints: dict[str, str] | None = None,
    provider: str = "local",
) -> Instance:
    return Instance(
        id=iid,
        provider=provider,
        tags={},
        created_at=0.0,
        cost_rate_usd_per_hr=0.0,
        status=status,
        endpoints=endpoints if endpoints is not None else {"http": "http://pod-1.x"},
    )


def _wak_for_ctx(ctx: SessionContext) -> str:
    cfg = ctx.cfg
    assert cfg is not None
    base_models = [m for m in cfg.models if m.kind == "base"]
    return WarmAttachKey(
        base_model=base_models[0].ref if base_models else "",
        engine=cfg.engine.kind if cfg.engine else "",
        precision=cfg.engine.precision if cfg.engine else "",
    ).derive()


def _stub_generate(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    def _gen(cfg: Any, request: Any, **kw: Any) -> tuple[Any, Any]:
        captured.update(kw)
        artifact = MagicMock()
        artifact.uri = "test://x"
        # Echo the supplied instance back (warm-attach path) or invent one
        # (cold-boot path) so the post-generate ledger.record branch fires
        # only when this was a cold boot.
        returned = kw.get("instance")
        if returned is None:
            returned = _fake_instance(iid="cold-pod-99")
        return (artifact, returned)

    monkeypatch.setattr("kinoforge.cli.generate", _gen, raising=False)
    monkeypatch.setattr("kinoforge.cli._commands.generate", _gen)


def _install_fake_provider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    instance: Instance | None,
    raise_get_instance: Exception | None = None,
) -> MagicMock:
    """Monkeypatch registry.get_provider("local") to return our fake."""
    provider = MagicMock()
    if raise_get_instance is not None:
        provider.get_instance.side_effect = raise_get_instance
    else:
        provider.get_instance.return_value = instance

    def _factory() -> Any:
        return provider

    from kinoforge.core import registry

    monkeypatch.setattr(registry, "get_provider", lambda _name: _factory)
    return provider


# ---------------------------------------------------------------------------
# Mutex tests
# ---------------------------------------------------------------------------


def test_attach_pod_and_no_reuse_mutex_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _cmd_generate(
        _make_args(attach_pod="pod-x", no_reuse=True),
        _make_ctx(tmp_path),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "--attach-pod" in err and "--no-reuse" in err
    assert "mutually exclusive" in err


def test_attach_pod_and_emit_provision_record_mutex_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _cmd_generate(
        _make_args(
            attach_pod="pod-x",
            emit_provision_record=tmp_path / "rec.json",
        ),
        _make_ctx(tmp_path),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "mutually exclusive" in err
    assert "attach does not provision" in err


# ---------------------------------------------------------------------------
# Ledger validation
# ---------------------------------------------------------------------------


def test_attach_pod_missing_from_ledger_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _cmd_generate(
        _make_args(attach_pod="ghost-pod"),
        _make_ctx(tmp_path),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "ghost-pod" in err
    assert "not in ledger" in err


def test_attach_pod_warm_attach_key_mismatch_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _make_ctx(tmp_path)
    ledger = ctx.ledger()
    inst = _fake_instance(iid="pod-wrong")
    ledger.record(inst)
    ledger.touch("pod-wrong", warm_attach_key="DIFFERENT_HEX")

    rc = _cmd_generate(_make_args(attach_pod="pod-wrong"), ctx)
    assert rc == 1
    err = capsys.readouterr().err
    assert "pod-wrong" in err
    assert "warm_attach_key" in err or "warm-attach key" in err


def test_attach_pod_status_not_ready_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = _make_ctx(tmp_path)
    ledger = ctx.ledger()
    inst = _fake_instance(iid="pod-dead")
    ledger.record(inst)
    ledger.touch("pod-dead", warm_attach_key=_wak_for_ctx(ctx))

    # Live probe says terminated.
    _install_fake_provider(
        monkeypatch, instance=_fake_instance(iid="pod-dead", status="terminated")
    )

    rc = _cmd_generate(_make_args(attach_pod="pod-dead"), ctx)
    assert rc == 1
    err = capsys.readouterr().err
    assert "pod-dead" in err
    assert "requires" in err and "ready" in err


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_attach_pod_happy_path_threads_instance_skips_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ctx(tmp_path)
    ledger = ctx.ledger()
    live = _fake_instance(iid="pod-warm")
    ledger.record(live)
    ledger.touch("pod-warm", warm_attach_key=_wak_for_ctx(ctx))
    _install_fake_provider(monkeypatch, instance=live)

    captured: dict[str, Any] = {}
    _stub_generate(monkeypatch, captured)

    rc = _cmd_generate(_make_args(attach_pod="pod-warm"), ctx)
    assert rc == 0
    # Instance forwarded — orchestrator path skips find_offers/create_instance.
    assert captured["instance"] is live
    # `single=False` so pod survives (no destroy on session exit).
    assert captured.get("single") is False


def test_attach_pod_happy_path_does_not_re_record_to_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-recording would double-count cost + violate ledger uniqueness."""
    ctx = _make_ctx(tmp_path)
    ledger = ctx.ledger()
    live = _fake_instance(iid="pod-warm")
    ledger.record(live)
    ledger.touch("pod-warm", warm_attach_key=_wak_for_ctx(ctx))
    _install_fake_provider(monkeypatch, instance=live)

    captured: dict[str, Any] = {}
    _stub_generate(monkeypatch, captured)
    _cmd_generate(_make_args(attach_pod="pod-warm"), ctx)

    assert len([e for e in ledger.entries() if e["id"] == "pod-warm"]) == 1


def test_attach_pod_loras_parses_before_attach_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--attach-pod` composes with `--loras` — the P3 heredoc parser runs
    BEFORE attach validation, so a successful invocation proves both
    surfaces compose without per-feature regression. (The deeper
    /lora/set_stack payload check belongs in the integration suite.)"""
    ctx = _make_ctx(tmp_path)
    ledger = ctx.ledger()
    live = _fake_instance(iid="pod-warm")
    ledger.record(live)
    ledger.touch("pod-warm", warm_attach_key=_wak_for_ctx(ctx))
    _install_fake_provider(monkeypatch, instance=live)

    captured: dict[str, Any] = {}
    _stub_generate(monkeypatch, captured)

    rc = _cmd_generate(
        _make_args(
            attach_pod="pod-warm",
            loras="civitai:42@99 0.5\n",
        ),
        ctx,
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "branch validation failed" not in err
    assert "--loras" not in err  # no parse-error report rendered


# ---------------------------------------------------------------------------
# Task 4: --emit-provision-record
# ---------------------------------------------------------------------------


def test_emit_provision_record_writes_json_after_cold_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ctx(tmp_path)
    captured: dict[str, Any] = {}
    _stub_generate(monkeypatch, captured)

    record_path = tmp_path / "rec.json"
    rc = _cmd_generate(_make_args(emit_provision_record=record_path), ctx)
    assert rc == 0
    assert record_path.exists()
    rec = json.loads(record_path.read_text())
    assert set(rec.keys()) == {
        "pod_id",
        "endpoint_url",
        "provider",
        "warm_attach_key",
        "provision_ts",
        "cost_per_hr_usd",
    }
    assert rec["pod_id"] == "cold-pod-99"
    assert rec["provider"] == "local"
    assert rec["warm_attach_key"] == _wak_for_ctx(ctx)
    # Local TZ ISO-8601 — must contain a TZ offset (+HH:MM or -HH:MM)
    # OR be a naive local time without "Z" (per feedback_local_timezone_only).
    assert "Z" not in rec["provision_ts"]


def test_emit_provision_record_not_written_on_provision_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ctx(tmp_path)

    def _gen_boom(cfg: Any, request: Any, **kw: Any) -> tuple[Any, Any]:
        raise RuntimeError("provision exploded")

    monkeypatch.setattr("kinoforge.cli.generate", _gen_boom, raising=False)
    monkeypatch.setattr("kinoforge.cli._commands.generate", _gen_boom)

    record_path = tmp_path / "rec.json"
    with pytest.raises(RuntimeError, match="provision exploded"):
        _cmd_generate(_make_args(emit_provision_record=record_path), ctx)
    assert not record_path.exists()


def test_emit_provision_record_writes_endpoint_url_from_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _make_ctx(tmp_path)

    def _gen(cfg: Any, request: Any, **kw: Any) -> tuple[Any, Any]:
        artifact = MagicMock()
        artifact.uri = "test://x"
        inst = _fake_instance(
            iid="cold-pod-42",
            endpoints={"http": "http://pod-42.runpod.net"},
        )
        return (artifact, inst)

    monkeypatch.setattr("kinoforge.cli.generate", _gen, raising=False)
    monkeypatch.setattr("kinoforge.cli._commands.generate", _gen)

    record_path = tmp_path / "rec.json"
    rc = _cmd_generate(_make_args(emit_provision_record=record_path), ctx)
    assert rc == 0
    rec = json.loads(record_path.read_text())
    assert rec["pod_id"] == "cold-pod-42"
    assert rec["endpoint_url"] == "http://pod-42.runpod.net"
