"""Every check's applies_to must survive a cfg with engine=None (image configs).

CheckRegistry.applicable calls applies_to on EVERY registered check, so a single
unguarded cfg.engine.kind dereference crashes `kinoforge doctor` and the
generate-path preflight for any image config.

The guards this file exercises (``cfg.engine is None or cfg.engine.kind ...``)
in ``checks/models.py`` and ``checks/loras.py`` already landed incidentally
during a prior type-narrowing migration — see task-3-report.md for the
mutation-testing evidence that this file actually catches their removal.
"""

from __future__ import annotations

import importlib

import pytest

from kinoforge.core.config import Config
from kinoforge.validation.registry import CheckRegistry, default_registry

IMAGE_CFG = {
    "image": {"engine": "fake", "prompt": "a cat", "spec": {"model": "m"}},
}

VIDEO_CFG = {
    "mode": "t2v",
    "engine": {
        "kind": "diffusers",
        "precision": "fp8",
        "diffusers": {
            "server_cmd": [
                "python",
                "-m",
                "kinoforge.engines.diffusers.servers.minimax_h3_server",
            ],
            "base_url": "http://localhost:8000",
        },
    },
    "models": [{"ref": "hf:org/repo", "kind": "base", "target": "diffusion_models"}],
    "loras": [{"ref": "hf:o/r:f.safetensors", "target": "transformer"}],
}

# (module, class) pairs confirmed against the source (Step 1 of the brief).
FIXED_CHECKS = [
    ("kinoforge.validation.checks.models", "ModelRefReachableCheck"),
    ("kinoforge.validation.checks.loras", "LoraServerSupportCheck"),
]


def _registry() -> CheckRegistry:
    """Return a CheckRegistry with every production check registered.

    Returns:
        The module-level default :class:`~kinoforge.validation.registry.CheckRegistry`,
        populated by importing ``kinoforge.validation.checks`` for its
        self-registration side effects first.

    Note:
        The brief this test derives from named ``kinoforge._adapters`` as the
        self-registration import. That import only wires engines/providers
        (see its own module docstring) and never touches
        ``kinoforge.validation.checks``, so ``default_registry()`` came back
        holding only the two checks that self-register as a SIDE EFFECT of an
        engine import (``runpod_capacity_hint``,
        ``skypilot_cloud_pin_supported``) — confirmed empirically: importing
        only ``kinoforge._adapters`` left ``ModelRefReachableCheck`` and
        ``LoraServerSupportCheck`` themselves unregistered, so
        ``test_the_registry_actually_had_checks_to_filter`` failed with 0
        applicable checks. ``tests/validation/test_field_support_check.py``'s
        ``test_both_checks_are_registered_for_doctor`` establishes the
        correct import: ``kinoforge.validation.checks`` (the package whose
        own docstring says importing it self-registers every built-in
        check).
    """
    import kinoforge.validation.checks  # noqa: F401  — self-registration side effect

    return default_registry()


def test_applicable_does_not_raise_on_an_image_cfg() -> None:
    """applies_to is called for every check, guarded or not.

    Bug this catches: validation/checks/models.py:83 and loras.py:58 reading
    cfg.engine.kind with no None guard, which makes `kinoforge doctor` on any
    image config die with AttributeError before a single check runs.
    """
    registry = _registry()
    cfg = Config.model_validate(IMAGE_CFG)
    applicable = registry.applicable(cfg)
    assert isinstance(applicable, list)


def test_the_registry_actually_had_checks_to_filter() -> None:
    """Guard the guard: a registry of zero checks passes the test above vacuously.

    Bug this catches: an import regression that leaves default_registry() empty,
    which would make the test above green while proving nothing at all.

    Threshold note: measured empirically at 6 applicable checks for VIDEO_CFG
    (``ledger_stale_rows``, ``lora_server_support``, ``lora_stack_conflict``,
    ``lora_refs_resolvable``, ``lora_engine_support``, ``model_ref_reachable``)
    via ``kinoforge.validation.checks`` + ``default_registry()`` — the import
    that actually self-registers the built-in checks (see ``_registry``'s
    docstring; the brief's originally-suggested ``kinoforge._adapters`` import
    left the registry holding only 2 unrelated provider-capability checks and
    0 applicable to VIDEO_CFG, which is exactly the vacuous-pass shape this
    test exists to catch). Asserting ``>= 4`` sits sensibly below the measured
    6 — tight enough to catch a registry collapse, loose enough to tolerate a
    future check legitimately not applying to this cfg shape.
    """
    registry = _registry()
    video_applicable = registry.applicable(Config.model_validate(VIDEO_CFG))
    assert len(video_applicable) >= 4, (
        f"expected the production registry to hold several checks applicable to "
        f"a plain video cfg; got {len(video_applicable)} — the registry is "
        f"probably empty, which would make the engine=None test vacuous"
    )


@pytest.mark.parametrize(("module_name", "class_name"), FIXED_CHECKS)
def test_fixed_predicates_return_false_not_raise(
    module_name: str, class_name: str
) -> None:
    """Each fixed applies_to answers False for an image cfg rather than raising.

    Bug this catches: "fixing" the crash by wrapping applies_to in a bare
    try/except, which would also swallow a genuine misconfiguration.
    """
    module = importlib.import_module(module_name)
    check = getattr(module, class_name)()
    assert check.applies_to(Config.model_validate(IMAGE_CFG)) is False
