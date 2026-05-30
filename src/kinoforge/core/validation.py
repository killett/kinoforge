"""Pure, role-authoritative request validation.

No I/O. No globals. No side effects.
"""

from __future__ import annotations

from dataclasses import replace

from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    MODE_ROLE_REQUIREMENTS,
    ConditioningAsset,
    GenerationRequest,
    ModelProfile,
)


def validate_request(
    profile: ModelProfile,
    request: GenerationRequest,
    *,
    accepted_kinds: set[str],
) -> GenerationRequest:
    """Validate a GenerationRequest against a ModelProfile and engine constraints.

    Applies the mode/role contract defined in ``MODE_ROLE_REQUIREMENTS`` and the
    engine's ``accepted_kinds`` allow-list. Returns a NEW ``GenerationRequest``
    with any auto-applied defaults (e.g. lone un-roled image in a single-asset mode
    receives the required role). Never mutates the input.

    Args:
        profile: The ``ModelProfile`` describing what this model can do.
        request: The ``GenerationRequest`` to validate.
        accepted_kinds: The set of asset ``kind`` values the engine accepts
            (e.g. ``{"image"}``). Assets whose kind is not in this set raise.

    Returns:
        A new ``GenerationRequest`` with auto-defaults applied and the original
        assets list and prompt otherwise preserved.

    Raises:
        ValidationError: If ``request.mode`` is not in ``profile.supported_modes``,
            if a required role is missing or duplicated, if a role's asset has the
            wrong kind, or if any asset kind is not in ``accepted_kinds``.
    """
    # --- 1. Mode gate --------------------------------------------------------
    if request.mode not in profile.supported_modes:
        raise ValidationError(
            f"mode {request.mode!r} not in supported_modes"
            f" ({sorted(profile.supported_modes)})"
        )

    # --- 2. Kind gate (cheap, before any copying) ----------------------------
    for asset in request.assets:
        if asset.kind not in accepted_kinds:
            raise ValidationError(
                f"asset kind {asset.kind!r} not accepted by engine"
                f" (accepted={sorted(accepted_kinds)})"
            )

    required_roles: set[str] = MODE_ROLE_REQUIREMENTS[request.mode]

    # --- 3. Single-asset-mode lone-image default ------------------------------
    # Copy the list so we never mutate the caller's data.
    assets: list[ConditioningAsset] = list(request.assets)

    if (
        len(required_roles) == 1
        and len(assets) == 1
        and assets[0].kind == "image"
        and assets[0].role == ""
    ):
        only_required_role = next(iter(required_roles))
        assets[0] = replace(assets[0], role=only_required_role)

    # --- 4. Role contract check ----------------------------------------------
    role_count: dict[str, int] = {}
    for asset in assets:
        role_count[asset.role] = role_count.get(asset.role, 0) + 1

    for role in required_roles:
        n = role_count.get(role, 0)
        if n == 0:
            raise ValidationError(
                f"mode {request.mode!r} requires role {role!r} (missing)"
            )
        if n > 1:
            raise ValidationError(
                f"mode {request.mode!r}: role {role!r} duplicated ({n}x)"
            )
        # All assets filling this role must have kind == "image"
        kinds_for_role = {a.kind for a in assets if a.role == role}
        if kinds_for_role != {"image"}:
            raise ValidationError(
                f"role {role!r} requires kind=='image'; got {sorted(kinds_for_role)}"
            )

    # --- 5. Return a new object with (possibly) rewritten assets -------------
    return replace(request, assets=assets)
