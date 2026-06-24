"""``_detect_moe_arity`` + ``_resolve_transformer`` — pure-function dispatch.

P2 §3 of docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md.
Pure functions + exception types — no FastAPI / pipeline / network state.
Every test names the concrete bug it catches.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from kinoforge.engines.diffusers.servers import wan_t2v_server

# Access exception classes + helpers via the LIVE module rather than
# importing at module top — other test modules (e.g.
# test_diffusers_wan_t2v_server.py) call ``importlib.reload(srv)``, after
# which top-level imports here would hold STALE class identities that
# ``pytest.raises`` no longer matches against the freshly-raised
# instances.


@pytest.fixture(autouse=True)
def _reset_pipe_arity() -> Iterator[None]:
    """Snapshot/restore module-level ``_pipe_arity`` around each test.

    Other test modules touch the cold-boot path which can mutate this
    global; without the reset the resolver tests pick up a stale arity
    when run in the full engine suite (test order pollution).
    """
    original = wan_t2v_server._pipe_arity
    yield
    wan_t2v_server._pipe_arity = original


class _SingleTransformerStub:
    """Mimics a non-MoE pipeline like Wan 2.1."""

    transformer = object()


class _MoEStub:
    """Mimics a Wan 2.2 dual-transformer pipeline."""

    transformer = object()
    transformer_2 = object()


def test_detect_moe_arity_single_transformer_returns_1() -> None:
    """Bug: detector miscounts when pipeline has only the bare
    ``transformer`` attribute — would route MoE-only paths through
    single-transformer branches and silently downgrade a Wan 2.2 swap to
    Wan 2.1 semantics."""
    assert wan_t2v_server._detect_moe_arity(_SingleTransformerStub()) == 1


def test_detect_moe_arity_dual_transformer_returns_2() -> None:
    """Bug: detector misses ``transformer_2`` — treats Wan 2.2 as
    single-transformer and silently drops every low-noise LoRA into the
    high-noise stage (the failure mode this whole P2 was opened to fix)."""
    assert wan_t2v_server._detect_moe_arity(_MoEStub()) == 2


def test_detect_moe_arity_wan21_class_default_transformer_2_is_none() -> None:
    """Bug: Wan 2.1's ``WanPipeline`` inherits
    ``_lora_loadable_modules = ["transformer", "transformer_2"]`` from
    ``WanLoraLoaderMixin`` AND carries ``transformer_2 = None`` as a
    class default (so MoE subclasses can override). The naive
    "attribute exists" scan reported arity=2 on Wan 2.1 and rejected
    every ``branch="auto"`` cold-boot with ``branch_auto_disallowed_on_moe``
    — Tier-3 live fire #2 (2026-06-23). Fix: skip modules whose value
    is ``None`` so the slot must actually be populated to count."""

    class _Wan21Shape:
        _lora_loadable_modules = ("transformer", "transformer_2")
        transformer = object()
        transformer_2 = None

    assert wan_t2v_server._detect_moe_arity(_Wan21Shape()) == 1


def test_detect_moe_arity_wan22_both_transformer_slots_populated() -> None:
    """Bug as above, MoE shape — both slots must be non-None and
    return arity=2."""

    class _Wan22Shape:
        _lora_loadable_modules = ("transformer", "transformer_2")
        transformer = object()
        transformer_2 = object()

    assert wan_t2v_server._detect_moe_arity(_Wan22Shape()) == 2


def test_detect_moe_arity_ignores_transformer_name_and_other_kin() -> None:
    """Bug: detector counts ANY attr matching the bare prefix
    ``transformer_*``, so a diffusers pipeline carrying
    ``transformer_name`` (string class constant on
    ``WanLoraLoaderMixin``) reports arity 2 on Wan 2.1 — auto-on-MoE
    rejection then misfires for every Wan-2.1-shape pipeline.

    The Tier-3 live fire (2026-06-23) surfaced this on a real
    ``WanPipeline`` reporting arity=3. Pattern fix only accepts
    ``transformer`` exact and ``transformer_<digits>``."""

    class _PipeWithKinAttrs:
        transformer = object()
        transformer_name = "transformer"
        transformer_blocks = 42
        transformer_2_name = "transformer_2"

    assert wan_t2v_server._detect_moe_arity(_PipeWithKinAttrs()) == 1


def test_detect_moe_arity_counts_transformer_2_but_not_transformer_2_name() -> None:
    """Bug as above, MoE shape — detector must count ``transformer`` and
    ``transformer_2`` (arity=2) but ignore ``transformer_name`` and
    other off-spec siblings."""

    class _MoEWithKinAttrs:
        transformer = object()
        transformer_2 = object()
        transformer_name = "transformer"

    assert wan_t2v_server._detect_moe_arity(_MoEWithKinAttrs()) == 2


def test_resolve_auto_on_single_transformer_returns_transformer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: arity-1 + auto routes the wrong attribute, leading to None
    deref or KeyError on the loader call. Should return the bare
    ``transformer`` attribute."""
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 1)
    pipe = _SingleTransformerStub()
    assert wan_t2v_server._resolve_transformer(pipe, "auto") is pipe.transformer


def test_resolve_high_noise_on_moe_returns_transformer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: routing maps ``high_noise`` to ``transformer_2``. Every LoRA
    trained against the high-noise stage lands in the wrong half of the
    MoE pipeline."""
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 2)
    pipe = _MoEStub()
    assert wan_t2v_server._resolve_transformer(pipe, "high_noise") is pipe.transformer


def test_resolve_low_noise_on_moe_returns_transformer_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: routing maps ``low_noise`` to ``transformer`` (the high-noise
    stage). Every Wan 2.2 LoRA recipe silently degrades to wrong-stage
    LoRA application."""
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 2)
    pipe = _MoEStub()
    assert wan_t2v_server._resolve_transformer(pipe, "low_noise") is pipe.transformer_2


def test_resolve_auto_on_moe_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug: server accepts ``auto`` on MoE and silently loads the LoRA
    into ``pipe.transformer`` only (the diffusers default). The user
    can't tell because the run completes and produces a video, but the
    LoRA is half-applied — exactly the Q1 Option-D failure mode."""
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 2)
    pipe = _MoEStub()
    with pytest.raises(wan_t2v_server.BranchAutoNotAllowedOnMoE):
        wan_t2v_server._resolve_transformer(pipe, "auto")


def test_resolve_explicit_branch_on_single_transformer_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: server silently collapses ``high_noise`` to ``transformer``
    on a Wan 2.1 pipeline (the Q5 lenient-collapse failure mode). The
    explicit-portability semantics of ``auto`` evaporate."""
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 1)
    pipe = _SingleTransformerStub()
    with pytest.raises(wan_t2v_server.BranchUnsupportedOnSingleTransformer):
        wan_t2v_server._resolve_transformer(pipe, "high_noise")


def test_resolve_unknown_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: Pydantic Literal should prevent any off-Literal value
    from reaching the resolver, but a future test stub or refactor that
    bypasses the validator must still get a loud rejection rather than a
    silent fallthrough returning the high-noise transformer by default."""
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 2)
    pipe = _MoEStub()
    with pytest.raises(wan_t2v_server.BranchUnknown):
        wan_t2v_server._resolve_transformer(pipe, "medium")
