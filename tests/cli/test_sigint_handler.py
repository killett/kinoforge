"""Two-press SIGINT handler — first sets token, second re-raises.

Phase 50 Task 5 — these tests pin the CLI signal-handler contract:

1. First ``SIGINT`` while the handler is installed sets the shared
   ``CancelToken`` and does NOT raise — the orchestrator + backends
   unwind cooperatively.
2. Second ``SIGINT`` restores the default handler and re-raises
   ``KeyboardInterrupt`` so the operator can always force-exit.

The tests save the prior SIGINT handler in a ``try/finally`` so a
failure here can never corrupt the global signal state for subsequent
tests in the same pytest process.
"""

from __future__ import annotations

import signal
from pathlib import Path

import pytest

from kinoforge.cli._main import _install_sigint_handler
from kinoforge.core import CancelToken


def test_first_signal_sets_token_no_raise() -> None:
    """First Ctrl-C sets the shared CancelToken and does NOT raise.

    Bug: today the CLI installs no SIGINT handler, so Ctrl-C surfaces as
    a raw KeyboardInterrupt mid-orchestration — the orchestrator's
    backend poll loop never observes the operator's intent and the pod
    leaks because the cleanup path runs only on graceful exit.
    """
    token = CancelToken()
    prior = signal.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        _install_sigint_handler(token)
        # First press: handler intercepts, flips the token, no raise.
        signal.raise_signal(signal.SIGINT)
        assert token.is_set() is True
    finally:
        signal.signal(signal.SIGINT, prior)


def test_second_signal_reraises_and_restores_default() -> None:
    """Second Ctrl-C restores SIG_DFL and re-raises KeyboardInterrupt.

    Bug: today there is no escape hatch — a wedged backend with no
    cancellation honoring could trap the operator in an unkillable
    process. The two-press contract guarantees a force-exit is always
    one Ctrl-C away once the cooperative drain begins.
    """
    token = CancelToken()
    prior = signal.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        _install_sigint_handler(token)
        # First press: sets token, no raise.
        signal.raise_signal(signal.SIGINT)
        # Second press: restores default handler, raises KeyboardInterrupt.
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGINT)
        # After re-raise, the default handler is back in place so a third
        # press would terminate the process the usual way (no token check).
        assert signal.getsignal(signal.SIGINT) is signal.SIG_DFL
    finally:
        signal.signal(signal.SIGINT, prior)


# ---------------------------------------------------------------------------
# Audit B6: the handler must be installed for EVERY cancellable subcommand
# ---------------------------------------------------------------------------


_STUB_CFG = """\
engine:
  kind: diffusers
  precision: fp8
models:
  - kind: base
    ref: hf:Wan-AI/Wan2.2-T2V
    target: diffusion_models
compute:
  provider: fake
  image: fake:latest
upscale:
  engine: seedvr2
  scale: 2x
  seedvr2:
    variant: 3B
    precision: fp8
"""


@pytest.mark.parametrize("cmd", ["generate", "batch", "upscale", "interpolate"])
def test_handler_installed_for_every_cancel_token_command(
    cmd: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every subcommand that threads ``ctx.cancel_token`` gets the handler.

    Bug caught (audit B6): ``_INTERRUPTIBLE_CMDS`` listed only
    generate/batch, but ``_cmd_upscale`` and ``_cmd_interpolate`` both
    pass ``ctx.cancel_token`` into the orchestrator. Ctrl-C on those two
    raised straight through the Phase-50 cooperative drain, skipping the
    teardown that destroys the pod — on exactly the ``--no-reuse``
    one-shot paths where nothing else reaps it.

    The parametrization is derived from the handlers that accept a
    cancel token, not copied from the allowlist, so a future cancellable
    subcommand that forgets to register fails here.
    """
    from kinoforge.cli import _main

    installed: list[object] = []
    monkeypatch.setattr(
        _main, "_install_sigint_handler", lambda token: installed.append(token)
    )
    monkeypatch.setitem(_main._DISPATCH, cmd, lambda _args, _ctx: 0)

    cfg = tmp_path / "c.yaml"
    cfg.write_text(_STUB_CFG)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00")
    manifest = tmp_path / "m.yaml"
    manifest.write_text("jobs: []\n")

    argv = {
        "generate": [cmd, "--config", str(cfg), "--mode", "t2v", "--prompt", "p"],
        "batch": [cmd, "--config", str(cfg), "--manifest", str(manifest)],
        "upscale": [cmd, "--config", str(cfg), "--video", str(video)],
        "interpolate": [cmd, "--config", str(cfg), "--video", str(video)],
    }[cmd]

    assert _main.main(argv) == 0
    assert len(installed) == 1, f"{cmd} dispatched without the SIGINT handler"
