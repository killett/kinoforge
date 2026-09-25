"""Behavior: a pod without PEFT must not advertise LoRA support.

U59, found LIVE on 2026-09-23 at the cost of a full H200 boot (~$0.42).
Diffusers' ``load_lora_weights`` / ``set_adapters`` / ``unload_lora_weights``
all require the PEFT backend at runtime, and nothing checked it was installed:
not the config schema, not the provisioner, not the server's own capability
probe. So pod ``run-20260923-001929`` booted to ready, ``/health`` reported
``lora.supported: true``, and the first ``/lora/set_stack`` returned
``{'error': 'lora_swap_failed', 'underlying': 'PEFT backend is required for
this method.', 'status': 500}``.

The example set is already guarded — ``test_diffusers_lora_configs_pip_install_
peft`` makes "any diffusers cfg with a non-empty ``loras:`` block must
pip-install ``peft``" a property of the examples — but an operator-authored
config outside ``examples/`` buys the same lesson at the same price. Only the
pod can answer whether PEFT is actually importable in the image it ended up
with, which is why the filing put the fix here rather than in another
config-load check.

``_lora_health``'s own docstring already stated the invariant this restores:
"a pod claiming support it cannot honour is the cross-boot lie the whole parity
check exists to prevent."
"""

from __future__ import annotations

import builtins
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _clear_probe_cache() -> Any:
    """Drop the memoised PEFT answer around each test.

    The probe is cached because it runs per ``/health`` and an import attempt
    per request is wasteful; that cache would otherwise leak a verdict from
    one test into the next.
    """
    from kinoforge.engines.diffusers.servers import _lora

    _lora.peft_available.cache_clear()
    yield
    _lora.peft_available.cache_clear()


def _hide_peft(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import peft`` raise, as it does on an image that lacks it."""
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "peft" or name.startswith("peft."):
            raise ImportError("No module named 'peft'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def test_the_probe_reports_false_when_peft_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe must actually attempt the import.

    Bug caught: checking ``importlib.util.find_spec`` only, or a version
    string, rather than importing. A package can be present on disk and still
    fail to import — a broken wheel, a torch/peft ABI mismatch — and the error
    that cost $0.42 was raised at CALL time by diffusers, not at install time.
    """
    from kinoforge.engines.diffusers.servers import _lora

    _hide_peft(monkeypatch)
    _lora.peft_available.cache_clear()

    assert _lora.peft_available() is False


def test_the_probe_reports_true_when_peft_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control — the probe must not be a constant ``False``.

    Bug caught: a probe that always fails would degrade every pod's
    ``lora.supported`` to false and make the whole LoRA feature unreachable
    — a worse outcome than the defect, and one that would be reverted rather
    than fixed.

    ``peft`` is a pod-only dependency and is not installed in the controller
    env, so a real import would skip this control and leave the healthy path
    unverified on every local and CI run — exactly the half that must not
    regress. A stub module in ``sys.modules`` makes the success branch
    genuinely executable here.
    """
    import sys
    import types

    from kinoforge.engines.diffusers.servers import _lora

    monkeypatch.setitem(sys.modules, "peft", types.ModuleType("peft"))
    _lora.peft_available.cache_clear()

    assert _lora.peft_available() is True


def test_health_does_not_claim_lora_support_without_peft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect. A pod that cannot apply a stack must not say it can.

    Bug caught: leaving ``_lora_health`` keyed only on the pipeline profile.
    The controller reads this block to decide whether to POST a stack, so a
    true here is what turns an unusable image into a 500 after the boot is
    already paid for.
    """
    from kinoforge.engines.diffusers.servers import _lora
    from kinoforge.engines.diffusers.servers import minimax_h3_server as srv

    _hide_peft(monkeypatch)
    _lora.peft_available.cache_clear()
    monkeypatch.setattr(srv.ready, "is_set", lambda: True)
    monkeypatch.setattr(
        srv,
        "lora_profile",
        type(
            "_P",
            (),
            {
                "targets": ("transformer",),
                "default_target": "transformer",
                "name": "minimax-h3-t2va",
            },
        )(),
    )

    block = srv._lora_health()

    assert block["supported"] is False
    assert block["targets"] == []


def test_health_says_WHY_support_was_withdrawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexplained false is nearly as expensive as a wrong true.

    Bug caught: degrading ``supported`` silently. An operator seeing
    ``supported: false`` on a pod whose profile plainly has a target would
    reasonably suspect the profile, the config, or the model — and go looking
    in three wrong places before finding a missing pip package. The reason
    string is what makes the boot recoverable at $0 instead of $0.42.
    """
    from kinoforge.engines.diffusers.servers import _lora
    from kinoforge.engines.diffusers.servers import minimax_h3_server as srv

    _hide_peft(monkeypatch)
    _lora.peft_available.cache_clear()
    monkeypatch.setattr(srv.ready, "is_set", lambda: True)
    monkeypatch.setattr(
        srv,
        "lora_profile",
        type(
            "_P",
            (),
            {
                "targets": ("transformer",),
                "default_target": "transformer",
                "name": "minimax-h3-t2va",
            },
        )(),
    )

    reason = str(srv._lora_health().get("reason", ""))

    assert "peft" in reason.lower()


def test_health_still_claims_support_when_peft_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control for the health block, not just the probe.

    Bug caught: wiring the probe in with the sense inverted, or letting it
    override a healthy pod. Every working LoRA config in the project depends
    on this staying true.
    """
    import sys
    import types

    from kinoforge.engines.diffusers.servers import _lora
    from kinoforge.engines.diffusers.servers import minimax_h3_server as srv

    # See `test_the_probe_reports_true_when_peft_imports`: peft is pod-only,
    # so stub it rather than skip and leave the healthy path untested.
    monkeypatch.setitem(sys.modules, "peft", types.ModuleType("peft"))
    _lora.peft_available.cache_clear()
    monkeypatch.setattr(srv.ready, "is_set", lambda: True)
    monkeypatch.setattr(
        srv,
        "lora_profile",
        type(
            "_P",
            (),
            {
                "targets": ("transformer",),
                "default_target": "transformer",
                "name": "minimax-h3-t2va",
            },
        )(),
    )

    block = srv._lora_health()

    assert block["supported"] is True
    assert block["targets"] == ["transformer"]
