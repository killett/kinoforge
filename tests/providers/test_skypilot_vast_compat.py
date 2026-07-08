"""apply_vast_sdk_compat bridges sky's vast adapter to vastai-sdk >= 0.2."""

from __future__ import annotations

import pytest

pytest.importorskip("vastai_sdk")  # only runs in the live-skypilot env


def test_shim_makes_client_api_key_resolve() -> None:
    # Bug caught: sky/provision/vast/utils.py:204 reads vast.vast().client.api_key
    # but vastai-sdk 0.2.5 has no .client, so every vast launch AttributeErrors.
    from kinoforge.providers.skypilot.vast_compat import apply_vast_sdk_compat

    apply_vast_sdk_compat()
    from vastai_sdk import (  # type: ignore[import-not-found, unused-ignore]  # noqa: I001
        VastAI,
    )

    assert VastAI(api_key="secret-key").client.api_key == "secret-key"


def test_shim_is_idempotent() -> None:
    # Bug caught: re-applying stacks properties / re-patches an already-good class.
    from kinoforge.providers.skypilot.vast_compat import apply_vast_sdk_compat

    apply_vast_sdk_compat()
    assert apply_vast_sdk_compat() is False
