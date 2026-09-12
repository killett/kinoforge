"""The server must name its installed torch build in the startup log.

Why this exists: U34 asked a one-line question — does the cu124 extra index
change the torch wheel a pod installs? — and it took three sessions, six pods
and a bespoke race-the-teardown harness to answer, because **nothing on the pod
ever says which torch it is running.** The version had to be scraped out of
pip's incidental chatter in ``/tmp/bootstrap.log`` via a RunPod-only port-8001
sidecar, inside the ~100 s the pod lives. On Modal that sidecar does not exist
at all, which is why 19 of the 20 torch-pinning cfgs are still unproven (U37).

One log line at startup makes the wheel readable from every provider's ordinary
log surface — RunPod's bootstrap.log, Modal's ``modal app logs`` — with no race,
no sidecar, and no harness.
"""

from __future__ import annotations

import importlib
import logging
import sys
import types
from typing import Any

import pytest


def _fake_torch(version: str, cuda: str | None) -> types.ModuleType:
    """Build a stand-in ``torch`` module exposing only what the log reads.

    torch is NOT installed in the dev environment (the server imports it lazily
    inside functions for exactly that reason), so the "torch present" cases have
    to supply one. This fake is a true external boundary — a 2 GB C-extension —
    and the values it carries are fixture data, chosen here rather than derived
    from the code under test.

    Args:
        version: Value for ``torch.__version__``.
        cuda: Value for ``torch.version.cuda``.

    Returns:
        A module object suitable for insertion into ``sys.modules``.
    """
    mod = types.ModuleType("torch")
    mod.__version__ = version  # type: ignore[attr-defined]
    ver = types.ModuleType("torch.version")
    ver.cuda = cuda  # type: ignore[attr-defined]
    mod.version = ver  # type: ignore[attr-defined]
    return mod


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Reload the server module so each test gets a clean startup hook.

    Args:
        monkeypatch: pytest's patcher, used to neutralise the pipeline load.

    Returns:
        The freshly reloaded server module.
    """
    import kinoforge.engines.diffusers.servers.wan_t2v_server as srv

    importlib.reload(srv)
    monkeypatch.setattr(srv, "_load_pipeline", lambda **_kw: object())
    monkeypatch.setattr(srv, "_register_eager_wan", lambda _p: None)
    # Let the real worker thread spawn against a no-op target rather than
    # faking threading itself: it exits immediately, and patching the
    # stdlib module through the server would reach every other test too.
    monkeypatch.setattr(srv, "_worker_loop", lambda: None)
    return srv


def _torch_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return startup log messages that mention torch.

    Args:
        caplog: pytest's log capture fixture.

    Returns:
        The rendered messages naming torch.
    """
    return [r.getMessage() for r in caplog.records if "torch" in r.getMessage()]


class TestTorchBuildIsLogged:
    """The installed wheel must be readable from an ordinary log line."""

    def test_logs_the_actual_installed_version_and_cuda_build(
        self,
        server: Any,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The line carries torch's real version string and CUDA build.

        Catches a banner that merely says "torch loaded", or one that hard-codes
        a version: the version STRING is the entire diagnostic, and the whole
        point of U34 was that ``2.6.0`` and ``2.6.0+cu124`` are different wheels
        that are otherwise indistinguishable in the log.
        """
        # A version no wheel will ever carry, on purpose: with a plausible
        # fixture like 2.6.0+cu124 this assertion would still pass against an
        # implementation that hard-codes that exact string, which is the bug it
        # is here to catch. Verified by falsification — hard-coding the value
        # leaves this test green only if the fixture agrees with the hard-code.
        monkeypatch.setitem(sys.modules, "torch", _fake_torch("9.9.9+cu999", "99.9"))
        with caplog.at_level(logging.INFO):
            server._startup()

        lines = _torch_lines(caplog)
        assert lines, "startup logged nothing about torch"
        assert any("9.9.9+cu999" in ln for ln in lines), lines
        assert any("99.9" in ln for ln in lines), lines

    def test_bare_wheel_is_distinguishable_from_the_cu_wheel(
        self,
        server: Any,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A bare PyPI wheel logs as bare, with no CUDA build invented.

        This is the negative control for the test above, and it is the exact
        pair U34 had to spend two live pods to separate: arm A's bare ``2.6.0``
        against arm B's ``2.6.0+cu124``. Catches an implementation that
        unconditionally appends a ``+cuXXX`` suffix, or that reports
        ``torch.cuda.is_available()`` (a fact about the HARDWARE) in place of
        the wheel's own build tag.
        """
        monkeypatch.setitem(sys.modules, "torch", _fake_torch("2.6.0", None))
        with caplog.at_level(logging.INFO):
            server._startup()

        lines = _torch_lines(caplog)
        assert lines, "startup logged nothing about torch"
        assert any("2.6.0" in ln for ln in lines), lines
        assert not any("cu124" in ln for ln in lines), (
            f"a bare wheel must not be reported as a CUDA wheel: {lines}"
        )

    def test_logged_in_upscale_only_mode(
        self,
        server: Any,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``KINOFORGE_SKIP_WAN_LOAD=1`` pods report their torch too.

        Catches putting the line inside the Wan-load branch — which returns
        early. That would silence exactly the upscale-only pods (spandrel,
        FlashVSR) that U34's two arms actually ran on, i.e. the configuration
        where this question gets asked.
        """
        monkeypatch.setenv("KINOFORGE_SKIP_WAN_LOAD", "1")
        monkeypatch.setitem(sys.modules, "torch", _fake_torch("2.6.0+cu124", "12.4"))
        with caplog.at_level(logging.INFO):
            server._startup()

        assert any("2.6.0+cu124" in ln for ln in _torch_lines(caplog)), (
            "upscale-only startup returned before naming torch"
        )

    def test_logged_before_the_pipeline_load_so_a_dying_pod_still_reports(
        self,
        server: Any,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pod that dies loading weights has already named its torch.

        Catches logging at the END of ``_startup``: a pod whose weight download
        or pipeline load explodes is precisely when the wheel matters most, and
        that ordering would report nothing at all for it.
        """

        def _boom(**_kw: object) -> object:
            raise RuntimeError("weights fetch died")

        monkeypatch.setattr(server, "_load_pipeline", _boom)
        monkeypatch.setitem(sys.modules, "torch", _fake_torch("2.6.0+cu124", "12.4"))
        with (
            caplog.at_level(logging.INFO),
            pytest.raises(RuntimeError, match="weights fetch died"),
        ):
            server._startup()

        assert any("2.6.0+cu124" in ln for ln in _torch_lines(caplog)), (
            "the torch line must be emitted before the pipeline load, not after"
        )

    def test_missing_torch_does_not_break_startup(
        self,
        server: Any,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unimportable torch degrades to a note, never to a crash.

        Not hypothetical: the dev environment itself has no torch, which is why
        the server imports it lazily everywhere else. Catches an unguarded
        ``import torch`` at startup, which would turn a diagnostic line into a
        fatal startup failure on any pod with a broken or absent torch — a
        logging nicety taking down the server it was added to explain.
        """
        monkeypatch.setenv("KINOFORGE_SKIP_WAN_LOAD", "1")
        monkeypatch.setitem(sys.modules, "torch", None)  # import raises ImportError
        with caplog.at_level(logging.INFO):
            server._startup()  # must not raise

        assert server.ready.is_set(), "startup must still complete without torch"
        assert _torch_lines(caplog), (
            "an absent torch must still be reported, not silent"
        )
