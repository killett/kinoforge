"""U33 — optional diffusers cfg fields must fall back to their defaults.

Every test in this file goes through ``Config.model_dump()`` rather than a
hand-built dict, because that is the whole defect. ``DiffusersEngineConfig``
declares ``image`` and ``pytorch_extra_index_url`` as ``str | None = None``,
and pydantic's ``model_dump()`` emits an unset optional as a **present key
with value None**. ``diffusers_cfg.get(key, DEFAULT)`` therefore never
returns ``DEFAULT`` — the key is there, it is just null — and
``str(None)`` puts the literal four characters ``None`` into the bootstrap
script.

``tests/engines/test_diffusers_render_provision.py`` misses this class
entirely: its ``_minimal_cfg()`` is a plain dict where the optional keys are
*absent*, so ``.get``'s default fires there and the tests pass against the
broken code. The production path is the dumped model, never the ad-hoc dict.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from kinoforge.core.config import load_config
from kinoforge.engines.diffusers import DiffusersEngine

#: The cu124 wheel index the engine documents as its default. Spelled out
#: literally rather than imported from ``_PYTORCH_EXTRA_INDEX_URL`` so that
#: changing the constant is a deliberate edit to this test, not a silent
#: no-op.
_CU124 = "https://download.pytorch.org/whl/cu124"

#: The stock RunPod torch 2.4 image the engine documents as its default,
#: spelled out for the same reason.
_STOCK_IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"

_EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "configs"


def _dumped_cfg(tmp_path: Path, **diffusers_overrides: Any) -> dict[str, Any]:
    """Build a minimal diffusers cfg and round-trip it through pydantic.

    Args:
        tmp_path: pytest tmp dir to write the YAML into.
        **diffusers_overrides: Extra keys merged into the
            ``engine.diffusers`` block, e.g. ``image="myorg/x:v1"``.

    Returns:
        The ``model_dump()`` of the loaded config — the same shape
        ``render_provision`` is handed in production.
    """
    doc: dict[str, Any] = {
        "mode": "t2v",
        "provider": "runpod",
        "engine": {
            "kind": "diffusers",
            "precision": "fp16",
            "diffusers": {
                "base_url": "http://localhost:8000",
                "pip": ["torch>=2.6,<2.9"],
                "server_cmd": ["python", "-m", "diffusers_server"],
                **diffusers_overrides,
            },
        },
        "models": [
            {
                "ref": "hf:Wan-AI/Wan2.1-T2V-1.3B-Diffusers",
                "kind": "base",
                "target": "checkpoints",
            }
        ],
    }
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return load_config(path).model_dump()


def _pip_line(script: str) -> str:
    """Return the single ``pip install -q`` line from a provision script."""
    lines = [ln for ln in script.split("\n") if ln.startswith("pip install -q")]
    assert len(lines) == 1, f"expected exactly one pip line, got {lines!r}"
    return lines[0]


def test_pytorch_extra_index_url_defaults_to_cu124_from_a_dumped_cfg(
    tmp_path: Path,
) -> None:
    """A cfg that omits the key must still get the cu124 index.

    Bug this catches: the present-but-None ``.get`` default. Before the
    fix this line rendered ``pip install -q --extra-index-url None ...``
    on **every** diffusers cfg, so ``_PYTORCH_EXTRA_INDEX_URL`` was dead
    code and no CUDA-specific wheel index was ever applied.
    """
    cfg = _dumped_cfg(tmp_path)
    assert cfg["engine"]["diffusers"]["pytorch_extra_index_url"] is None, (
        "precondition: the dumped cfg must carry the key as present-and-None, "
        "otherwise this test is not exercising the defect"
    )

    rp = DiffusersEngine(probe_profile=None).render_provision(cfg)  # type: ignore[arg-type]

    assert f"--extra-index-url {_CU124} " in _pip_line(rp.script), _pip_line(rp.script)


def test_pytorch_extra_index_url_override_still_wins(tmp_path: Path) -> None:
    """An explicit cfg value must beat the default.

    Bug this catches: a fix that reaches for the constant unconditionally
    and drops the override — which would silently break the cu128 pin the
    FlashVSR BSA wheel needs, the exact case the override exists for.
    """
    cu128 = "https://download.pytorch.org/whl/cu128"
    cfg = _dumped_cfg(tmp_path, pytorch_extra_index_url=cu128)

    rp = DiffusersEngine(probe_profile=None).render_provision(cfg)  # type: ignore[arg-type]

    line = _pip_line(rp.script)
    assert f"--extra-index-url {cu128} " in line, line
    assert _CU124 not in line, f"default leaked past the override: {line!r}"


def test_image_defaults_to_stock_runpod_image_from_a_dumped_cfg(
    tmp_path: Path,
) -> None:
    """A cfg that omits ``image`` must get the documented default.

    Bug this catches: the latent sibling of the defect above, on the same
    model and the same call shape. ``str(diffusers_cfg.get("image",
    _DEFAULT_RUNPOD_IMAGE))`` returns the literal string ``"None"`` for an
    unset optional, so a diffusers cfg that does not name an image asks
    the provider to pull a container called ``None``. Masked today only
    because every shipped cfg happens to set ``image`` explicitly.
    """
    cfg = _dumped_cfg(tmp_path)
    assert cfg["engine"]["diffusers"]["image"] is None, (
        "precondition: the dumped cfg must carry image as present-and-None"
    )

    rp = DiffusersEngine(probe_profile=None).render_provision(cfg)  # type: ignore[arg-type]

    assert rp.image == _STOCK_IMAGE


def test_image_override_still_wins(tmp_path: Path) -> None:
    """An explicit image must beat the default.

    Bug this catches: a fix that collapses to the constant and ignores the
    cfg, which would strand every FlashVSR cfg on the cu124 image its BSA
    wheel cannot load.
    """
    cfg = _dumped_cfg(tmp_path, image="myorg/diffusers-base:v1")

    rp = DiffusersEngine(probe_profile=None).render_provision(cfg)  # type: ignore[arg-type]

    assert rp.image == "myorg/diffusers-base:v1"


def _shipped_diffusers_cfgs() -> list[Path]:
    """Every shipped example cfg whose engine is ``diffusers``."""
    found: list[Path] = []
    for path in sorted(_EXAMPLES.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        engine = doc.get("engine")
        if isinstance(engine, dict) and engine.get("kind") == "diffusers":
            found.append(path)
    return found


@pytest.mark.parametrize("cfg_path", _shipped_diffusers_cfgs(), ids=lambda p: p.name)
def test_no_shipped_cfg_renders_the_literal_none_as_a_flag_value(
    cfg_path: Path,
) -> None:
    """Class guard: ``None`` is never a legitimate shell argument.

    Bug this catches: any ``str(cfg.get(key, DEFAULT))`` over an optional
    pydantic field whose value reaches the bootstrap script — the
    ``--extra-index-url None`` instance, and every future one nobody has
    thought of yet. The assertion needs no knowledge of which flags exist:
    a flag followed by the bare word ``None`` is wrong whatever the flag
    is, because a real value would have been a URL, a path, or a number.
    """
    rp = DiffusersEngine(probe_profile=None).render_provision(  # type: ignore[arg-type]
        load_config(cfg_path).model_dump()
    )

    offenders = [
        ln
        for ln in rp.script.split("\n")
        if any(
            tok == "None" and idx > 0 and ln.split()[idx - 1].startswith("-")
            for idx, tok in enumerate(ln.split())
        )
    ]
    assert not offenders, (
        f"{cfg_path.name} renders a flag whose value is the literal string "
        f"'None' — an unset optional pydantic field leaked into the script:\n"
        + "\n".join(offenders)
    )
