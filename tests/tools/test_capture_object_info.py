"""Unit tests for :mod:`tools.capture_object_info` helpers.

Module-level imports of ``capture_object_info`` are safe — sys.path is
fixed up at module top and ``safe_print`` is the only top-level import.
The CLI ``main()`` defers all heavy kinoforge imports until after the
env-gate passes.
"""

from __future__ import annotations


class _StubEngine:
    """Minimal stand-in for ``GenerationEngine``.

    The stub starts with ``requires_local_weights = True`` because the
    bypass helper's contract is "flip True → False"; a stub that started
    at ``False`` would never exercise the flip and the assertion would
    be vacuous. The real ``ComfyUIEngine`` class default is now ``False``
    (PROGRESS B20 / today's fix), so callers that DO want a local-DL
    engine for upload-from-local cases would have to subclass and set
    ``True`` explicitly — and the bypass helper still needs to work for
    that subclass too.

    Instantiating a real engine here would require a probe profile + the
    full FakeBackend wiring; the bypass helper only reads + writes
    ``requires_local_weights``, so a stub captures the exact invariant
    under test without dragging the dependency graph in.
    """

    requires_local_weights: bool = True


def test_bypass_local_weights_download_flips_engine_flag() -> None:
    """Bug it catches: a future maintainer renames the engine flag
    (e.g. ``needs_local_weights``, ``requires_weights_local``) and the
    helper silently no-ops, restoring the 24-GB local-download crash
    behavior. The post-call assertion pins the contract: after this
    helper runs, ``provisioner.provision`` MUST skip the local download
    branch (``if engine.requires_local_weights: downloader(...)`` →
    ``if False: downloader(...)``).
    """
    from tools.capture_object_info import _bypass_local_weights_download

    engine = _StubEngine()
    assert engine.requires_local_weights is True  # baseline matches class default

    _bypass_local_weights_download(engine)

    assert engine.requires_local_weights is False


def test_bypass_local_weights_download_does_not_replace_engine_identity() -> None:
    """Bug it catches: a refactor that replaces ``engine.requires_local_weights
    = False`` with ``engine = replace(engine, ...)`` or
    ``return new_engine`` — both would silently break the caller in
    ``main()`` which keeps using the pre-call engine reference for the
    subsequent ``_provision_instance_and_build_backend`` call. The helper
    must mutate in place; the caller does NOT re-bind.
    """
    from tools.capture_object_info import _bypass_local_weights_download

    engine = _StubEngine()
    original_id = id(engine)

    _bypass_local_weights_download(engine)

    # Same object identity AND mutated state — pins the in-place contract.
    assert id(engine) == original_id
    assert engine.requires_local_weights is False


def test_bypass_local_weights_download_against_real_comfyui_engine() -> None:
    """Bug it catches: the stub-based tests above pass while
    ComfyUIEngine specifically has ``requires_local_weights`` as a
    ``Final``-typed or ``@property`` that rejects the assignment. This
    test exercises the actual production engine class to guarantee the
    monkey-patch lands.

    As of PROGRESS B20 the class default is False, so the production
    engine already arrives at the desired state. The bypass helper is
    a no-op for the default case here — we still call it and assert
    False post-call to lock the contract for any future subclass that
    flips the default back to True (e.g. an upload-from-local variant).
    """
    # Defer import to avoid pulling kinoforge.engines into module-load
    # cost for callers running only the stub tests above.
    from kinoforge.engines.comfyui import ComfyUIEngine
    from tools.capture_object_info import _bypass_local_weights_download

    engine = ComfyUIEngine(probe_profile=None)  # type: ignore[arg-type]
    # New baseline — class default is False (B20 fix).
    assert engine.requires_local_weights is False

    _bypass_local_weights_download(engine)

    # Helper must leave it False regardless of starting value.
    assert engine.requires_local_weights is False
