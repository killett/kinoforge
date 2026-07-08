"""Compat shim for SkyPilot's vast adapter vs vastai-sdk >= 0.2.

sky 0.12.3.post1's vast provisioner reads ``vast.vast().client.api_key``
(``sky/provision/vast/utils.py:204``). vastai-sdk 0.2.5 refactored the client:
``VastAI`` exposes ``.api_key`` directly and has no ``.client`` attribute, so the
old accessor AttributeErrors and every vast launch dies. This shim adds a
``client`` property that returns ``self`` so ``.client.api_key`` resolves to
``.api_key``. Idempotent + self-disabling: a no-op when ``VastAI`` already
resolves ``.client`` (a real client or a prior patch) or when ``vastai_sdk`` is
absent (the default pixi env has no vast SDK).
"""

from __future__ import annotations


def apply_vast_sdk_compat() -> bool:
    """Patch ``vastai_sdk.VastAI`` so ``.client.api_key`` resolves.

    Returns:
        ``True`` if the patch was applied this call; ``False`` if it was
        unnecessary (already resolvable) or ``vastai_sdk`` is unavailable.
    """
    try:
        from vastai_sdk import (  # type: ignore[import-not-found, unused-ignore]  # noqa: I001
            VastAI,
        )
    except Exception:  # noqa: BLE001 — sdk absent (default env) → nothing to patch
        return False
    if getattr(VastAI, "client", None) is not None:
        return False  # real client attr or prior patch → leave untouched
    VastAI.client = property(lambda self: self)  # type: ignore[attr-defined, unused-ignore]
    return True
