"""Cross-engine ABC contract test for model_identity.

Layer 8.  Iterates every registered GenerationEngine and ImageEngine, asserts
each exposes ``model_identity`` and calling it on an empty cfg returns ``""``
without raising.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

# Trigger self-registration of every engine.
import kinoforge._adapters  # noqa: F401
import kinoforge.image_engines.fake  # noqa: F401
import kinoforge.image_engines.fal  # noqa: F401
from kinoforge.core import registry


def _all_video_engine_factories() -> list[Callable[[], Any]]:
    return list(registry._engines.values())  # noqa: SLF001


def _all_image_engine_factories() -> list[Callable[[], Any]]:
    return list(registry._image_engines.values())  # noqa: SLF001


@pytest.mark.parametrize(
    "factory",
    _all_video_engine_factories(),
    ids=lambda f: getattr(f, "__name__", str(f)),
)
def test_every_video_engine_implements_model_identity(
    factory: Callable[[], Any],
) -> None:
    """Every registered video engine exposes model_identity that returns '' on empty cfg.

    Bug catch: a future engine ships without implementing the ABC method, or
    returns a non-string (e.g. None), silently breaking OutputSink slug writes.
    """
    eng = factory()
    assert hasattr(eng, "model_identity")
    assert callable(eng.model_identity)
    out = eng.model_identity({})
    assert isinstance(out, str), (
        f"{type(eng).__name__}.model_identity returned {type(out)}"
    )
    assert out == "", f"{type(eng).__name__}.model_identity({{}}) returned {out!r}"


@pytest.mark.parametrize(
    "factory",
    _all_image_engine_factories(),
    ids=lambda f: getattr(f, "__name__", str(f)),
)
def test_every_image_engine_implements_model_identity(
    factory: Callable[[], Any],
) -> None:
    """Every registered image engine exposes model_identity that returns '' on empty cfg.

    Bug catch: a future image engine ships without implementing the ABC method,
    or returns a non-string, silently breaking OutputSink slug writes for keyframes.
    """
    eng = factory()
    assert hasattr(eng, "model_identity")
    assert callable(eng.model_identity)
    out = eng.model_identity({})
    assert isinstance(out, str)
    assert out == ""
