"""Confirm SeedVR2Engine self-registers via _adapters import (T16).

The registry must surface ``"seedvr2"`` as an upscaler name on a cold
``import kinoforge`` so the CLI and orchestrator can route
``cfg.upscale.engine == "seedvr2"`` via ``registry.get_upscaler`` —
without an explicit ``import kinoforge.upscalers.seedvr2`` at every
call site.
"""

from __future__ import annotations


def test_seedvr2_registered_after_kinoforge_import() -> None:
    # Bug caught: someone removes the seedvr2 import line from
    # _adapters.py thinking it's unused → cfg.upscale.engine="seedvr2"
    # routes through registry.get_upscaler and raises UnknownAdapter
    # at orchestrator stage-assembly time. No CLI-level test catches
    # this because the CLI never imports kinoforge.upscalers.seedvr2
    # directly.
    import kinoforge._adapters  # noqa: F401 — self-registration hub
    from kinoforge.core import registry

    assert "seedvr2" in registry.upscaler_names()


def test_seedvr2_factory_returns_upscaler_instance() -> None:
    # Bug caught: registry.register_upscaler accepts the class but
    # stores it under the wrong name (typo) or wraps it incorrectly,
    # so calling the factory returns something that isn't a
    # UpscalerEngine. Verifies the factory chain end-to-end.
    import kinoforge._adapters  # noqa: F401
    from kinoforge.core import registry
    from kinoforge.core.interfaces import UpscalerEngine

    factory = registry.get_upscaler("seedvr2")
    engine = factory()
    assert isinstance(engine, UpscalerEngine)
    assert engine.name == "seedvr2"


def test_both_seedvr2_and_spandrel_registered() -> None:
    # Bug caught: a future edit to _adapters.py drops one of the two
    # upscaler imports, silently removing it from the registry.
    import kinoforge._adapters  # noqa: F401
    from kinoforge.core import registry

    names = registry.upscaler_names()
    assert "seedvr2" in names
    assert "spandrel" in names
