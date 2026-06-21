"""kinoforge generate/batch --dry-run-swap — matcher-decision preview.

Pins:
- The flag is wired on both ``generate`` and ``batch`` subparsers.
- ``_cmd_generate`` with ``dry_run_swap=True`` short-circuits BEFORE
  validate_for_generate, sink/store construction, or any
  backend/provider HTTP call. Exit 0.
- Output contains either the chosen pod_id + swap-plan summary OR the
  "no warm candidate, would cold-boot" message.
- No pod-lock acquisition leaks across calls: a second dry-run against
  the same cfg+ledger MUST still preview the same pod.
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from kinoforge.cli._commands import _cmd_batch, _cmd_generate
from kinoforge.cli._main import _build_parser
from kinoforge.core.interfaces import CapabilityKey


class _FakeLedger:
    """Surface-compatible enough for find_warm_attach_candidate."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries

    def find_pods_by_warm_attach_key(self, wak_hex: str) -> list[dict[str, Any]]:
        return [e for e in self._entries if e.get("warm_attach_key_hex") == wak_hex]

    def entries(self) -> list[dict[str, Any]]:
        return list(self._entries)


class _FakeCfg:
    def __init__(self, key: CapabilityKey) -> None:
        self._key = key
        self.compute = None
        self.models: list[Any] = []
        self._lifecycle = type("L", (), {"lora_swap_re_probe_after_s": 300.0})()

    def capability_key(self) -> CapabilityKey:
        return self._key

    def lifecycle(self) -> Any:
        return self._lifecycle


class _FakeCtx:
    def __init__(self, cfg: _FakeCfg, ledger: _FakeLedger) -> None:
        self.cfg = cfg
        self._ledger = ledger
        self.state_dir = None
        self.cancel_token = None

    def ledger(self) -> _FakeLedger:
        return self._ledger

    def store(self) -> Any:  # pragma: no cover — must NOT be called in dry-run
        raise AssertionError("store() called during --dry-run-swap")


def _key_two_loras() -> CapabilityKey:
    return CapabilityKey(
        base_model="hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers",
        loras=("civitai:2197303@2474081", "civitai:2197303@2474073"),
        engine="diffusers",
        precision="fp16",
    )


def _generate_args(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "config": "ignored",
        "prompt": "ignored",
        "mode": "t2v",
        "run_id": None,
        "output_dir": None,
        "no_output_dir": False,
        "instance_id": None,
        "force_attach": False,
        "no_reuse": False,
        "skip_preflight": True,
        "dry_run_swap": True,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _batch_args(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "config": "ignored",
        "manifest": "ignored",
        "batch_id": None,
        "concurrent": None,
        "env_file": None,
        "stream_format": "human",
        "output_dir": None,
        "no_output_dir": False,
        "instance_id": None,
        "force_attach": False,
        "no_reuse": False,
        "dry_run_swap": True,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_parser_accepts_dry_run_swap_on_generate_and_batch() -> None:
    """Flag wired on BOTH subparsers.

    Bug: flag only added to generate; batch dispatch crashes with
    AttributeError when the orchestrator dereferences args.dry_run_swap.
    """
    parser = _build_parser()
    gen_args = parser.parse_args(
        [
            "generate",
            "-c",
            "x.yaml",
            "--prompt",
            "p",
            "--mode",
            "t2v",
            "--dry-run-swap",
        ]
    )
    assert gen_args.dry_run_swap is True
    batch_args = parser.parse_args(
        [
            "batch",
            "-c",
            "x.yaml",
            "--manifest",
            "m.jsonl",
            "--dry-run-swap",
        ]
    )
    assert batch_args.dry_run_swap is True


def test_dry_run_swap_no_candidate_prints_cold_boot(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty ledger → matcher returns None → print "would cold-boot".

    Bug: handler prints the matcher object's repr instead of a
    human-readable line; or worse, crashes on None deref.
    """
    cfg = _FakeCfg(_key_two_loras())
    ctx = _FakeCtx(cfg, _FakeLedger([]))
    rc = _cmd_generate(_generate_args(), ctx)  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert rc == 0
    assert "cold-boot" in out
    assert "matcher" in out.lower()


def test_dry_run_swap_match_prints_pod_and_plan(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pod present, exact-match → print pod id + empty plan + cost 0.

    Bug: handler prints the plan but forgets the pod id, or swallows
    the swap_plan entirely so the operator can't tell what's about to
    happen.
    """
    key = _key_two_loras()
    cap_hex = key.derive()
    wak_hex = key.warm_attach_key().derive()
    ledger = _FakeLedger(
        [
            {
                "id": "pod-warm-1",
                "warm_attach_key_hex": wak_hex,
                "capability_key_hex": cap_hex,
                "lora_inventory": [
                    {
                        "ref": r,
                        "filename": f"{r}.s",
                        "size_bytes": 1,
                        "downloaded_at_local": "2026-06-20T10:00:00-07:00",
                        "last_used_at_local": "2026-06-20T10:00:00-07:00",
                        "adapter_name": f"lora_{i}",
                    }
                    for i, r in enumerate(key.lora_stack().refs)
                ],
                "loras_dir_free_bytes": 10_000_000,
                "loras_dir_free_bytes_observed_at_local": "2026-06-20T10:00:00-07:00",
            }
        ]
    )
    ctx = _FakeCtx(_FakeCfg(key), ledger)
    rc = _cmd_generate(_generate_args(), ctx)  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert rc == 0
    assert "pod-warm-1" in out
    assert "evict" in out.lower()
    assert "download" in out.lower()


def test_dry_run_swap_does_not_acquire_pod_lock(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two consecutive previews must both match the same pod.

    Bug: handler uses the real PodLockRegistry singleton, so the first
    dry-run leaves the lock held → second dry-run sees the pod as busy
    and falls through to cold-boot, misleading the operator about
    capacity.
    """
    key = _key_two_loras()
    cap_hex = key.derive()
    wak_hex = key.warm_attach_key().derive()
    ledger = _FakeLedger(
        [
            {
                "id": "pod-warm-1",
                "warm_attach_key_hex": wak_hex,
                "capability_key_hex": cap_hex,
                "lora_inventory": [],
                "loras_dir_free_bytes": 10_000_000,
                "loras_dir_free_bytes_observed_at_local": "2026-06-20T10:00:00-07:00",
            }
        ]
    )
    ctx = _FakeCtx(_FakeCfg(key), ledger)
    _cmd_generate(_generate_args(), ctx)  # type: ignore[arg-type]
    capsys.readouterr()  # drain
    rc = _cmd_generate(_generate_args(), ctx)  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert rc == 0
    assert "pod-warm-1" in out, "second preview must still see the pod"


def test_dry_run_swap_skips_preflight_and_backend(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No backend, no provider HTTP, no validate_for_generate call.

    Bug: the dry-run early-return is placed AFTER validate_for_generate,
    so an offline operator with stale creds eats a NETWORK preflight
    failure for a no-op preview.
    """
    import kinoforge.cli._commands as cmd

    # If validate_for_generate ever fires, fail loudly.
    def _explode(*a: Any, **k: Any) -> None:  # pragma: no cover
        raise AssertionError("validate_for_generate ran in --dry-run-swap")

    monkeypatch.setattr(
        "kinoforge.validation.validate_for_generate", _explode, raising=False
    )
    # Also ensure no orchestrator generate import attempt fires.
    monkeypatch.setattr(cmd, "generate", _explode, raising=False)

    ctx = _FakeCtx(_FakeCfg(_key_two_loras()), _FakeLedger([]))
    rc = _cmd_generate(_generate_args(skip_preflight=False), ctx)  # type: ignore[arg-type]
    assert rc == 0
    assert "cold-boot" in capsys.readouterr().out


def test_dry_run_swap_works_on_batch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``_cmd_batch`` honors --dry-run-swap symmetrically.

    Bug: dispatch only patched on generate; batch goes through full
    manifest-load + warm-scan path even in preview mode.
    """
    cfg = _FakeCfg(_key_two_loras())
    ctx = _FakeCtx(cfg, _FakeLedger([]))
    rc = _cmd_batch(_batch_args(), ctx)  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert rc == 0
    assert "cold-boot" in out
