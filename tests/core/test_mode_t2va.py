"""Behavior: t2va is a first-class mode in the role contract.

The mode gate (``profile.supported_modes``) and the role contract
(``MODE_ROLE_REQUIREMENTS``) are two SEPARATE checks inside ``validate_request``,
and they fail in different ways. A profile that declares t2va passes the first and
then ``KeyError``s on the second if the map has no entry — and a ``KeyError`` is not
a ``ValidationError``, so the orchestrator's ``except ValidationError`` teardown
does not fire and the pod is left running and billing.
"""

from __future__ import annotations

import pytest

from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    MODE_ROLE_REQUIREMENTS,
    GenerationRequest,
    ModelProfile,
)
from kinoforge.core.validation import validate_request


def _profile(modes: set[str]) -> ModelProfile:
    """Return a MiniMax-H3-shaped profile declaring *modes*.

    Args:
        modes: The value for ``supported_modes``.

    Returns:
        A ``ModelProfile`` carrying H3's real geometry.
    """
    return ModelProfile(
        name="minimax-h3",
        max_frames=360,
        fps=24,
        supported_modes=modes,
        max_resolution=(1344, 768),
        supports_native_extension=False,
        supports_joint_audio=True,
    )


def test_t2va_requires_no_conditioning_roles() -> None:
    """t2va takes no image roles — the same shape as t2v.

    Bug caught: t2va reaches a profile's ``supported_modes`` but never
    ``MODE_ROLE_REQUIREMENTS``. The mode gate passes, then
    ``MODE_ROLE_REQUIREMENTS[request.mode]`` raises ``KeyError`` mid-orchestration.
    """
    assert MODE_ROLE_REQUIREMENTS["t2va"] == {}


def test_validate_request_accepts_t2va_with_no_assets() -> None:
    """A text-only t2va request survives validation unchanged.

    Bug caught: as above, but through the real entry point rather than the map, so
    a future refactor that stops consulting the map for empty-role modes still has
    to keep this path working.
    """
    request = GenerationRequest(prompt="a fox in snow", mode="t2va")
    validated = validate_request(_profile({"t2va"}), request, accepted_kinds={"image"})
    assert validated.mode == "t2va"
    assert validated.assets == []
    assert validated.prompt == "a fox in snow"


def test_validate_request_rejects_t2va_on_a_profile_that_does_not_declare_it() -> None:
    """The mode gate still gates.

    Bug caught: t2va is made to work by widening the role map AND loosening the
    mode gate, so every model claims to do joint audio. Wan configs must keep
    rejecting t2va.
    """
    request = GenerationRequest(prompt="a fox in snow", mode="t2va")
    with pytest.raises(ValidationError, match=r"not in supported_modes"):
        validate_request(_profile({"t2v"}), request, accepted_kinds={"image"})
