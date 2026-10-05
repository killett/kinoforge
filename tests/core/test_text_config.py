"""The `text:` block (design §3): shape, the §3.3 refusals, the stage term."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from kinoforge.core.config import Config, load_config
from kinoforge.core.errors import ConfigError

_BASE: dict[str, Any] = {
    "engine": {
        "kind": "diffusers",
        "precision": "bf16",
        "diffusers": {
            "server_cmd": [
                "python",
                "-m",
                "kinoforge.engines.diffusers.servers.text_server",
            ],
            "capability": {"supported_modes": ["t2t"]},
        },
    },
    "models": [{"kind": "base", "ref": "hf:Qwen/Qwen3-0.6B", "target": "checkpoints"}],
    "text": {"engine": "transformers", "params": {"max_new_tokens": 32}},
    "compute": {"provider": "fake", "image": "fake:latest"},
}


def _cfg(**overrides: Any) -> dict[str, Any]:
    raw = copy.deepcopy(_BASE)
    raw.update(overrides)
    return raw


def test_valid_text_config_loads() -> None:
    """Bug caught: `text:` rejected as an unknown key, or port defaulting wrongly."""
    cfg = Config.model_validate(_cfg())
    assert cfg.text is not None
    assert cfg.text.engine == "transformers"
    assert cfg.text.port == 8000
    assert cfg.text.params == {"max_new_tokens": 32}
    assert cfg.text.system is None


def test_text_block_forbids_unknown_keys() -> None:
    """Bug caught: pydantic's default extra='ignore' dropping a typo'd `prmpt:`."""
    raw = _cfg()
    raw["text"]["prmpt"] = "x"
    with pytest.raises((ConfigError, ValueError), match="prmpt"):
        Config.model_validate(raw)


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (
            lambda r: r["engine"].__setitem__("kind", "comfyui"),
            "engine.kind == 'diffusers'",
        ),
        (
            lambda r: r["engine"]["diffusers"].pop("capability"),
            "capability.supported_modes",
        ),
        (
            lambda r: r["engine"]["diffusers"]["capability"].__setitem__(
                "supported_modes", []
            ),
            "capability.supported_modes",
        ),
        (
            lambda r: r["engine"]["diffusers"]["capability"].__setitem__(
                "supported_modes", ["t2v"]
            ),
            "not text modes",
        ),
        (lambda r: r.__setitem__("mode", "t2t"), "top-level `mode:` must be absent"),
        (
            lambda r: r.__setitem__(
                "upscale",
                {
                    "engine": "spandrel",
                    "scale": "2x",
                    "spandrel": {
                        "model_url": "hf:a/b/c.pth",
                        "arch": "realesrgan",
                        "precision": "fp16",
                        "tile_size": 512,
                        "batch_size": 4,
                    },
                },
            ),
            "`upscale:` is not allowed",
        ),
        (
            lambda r: r.__setitem__(
                "interpolate",
                {"engine": "rife", "fps": 60, "rife": {"weights_ref": "hf:a/b"}},
            ),
            "`interpolate:` is not allowed",
        ),
        (
            lambda r: r.__setitem__(
                "keyframe", {"engine": "fake", "prompt": "p", "spec": {"model": "m"}}
            ),
            "`keyframe:` is not allowed",
        ),
        (
            lambda r: r.__setitem__("loras", [{"ref": "hf:a/b", "strength": 1.0}]),
            "`loras:` is not allowed",
        ),
        (lambda r: r.__setitem__("models", []), "exactly one `kind: base`"),
        (
            lambda r: r["models"].append(
                {"kind": "base", "ref": "hf:x/y", "target": "checkpoints"}
            ),
            "exactly one `kind: base`",
        ),
    ],
    ids=[
        "non-diffusers-engine",
        "no-capability",
        "empty-modes",
        "video-mode",
        "top-level-mode",
        "upscale",
        "interpolate",
        "keyframe",
        "loras",
        "zero-base",
        "two-base",
    ],
)
def test_each_text_rule_refuses_and_names_the_key(mutate: Any, needle: str) -> None:
    """One case per §3.3 rule. Bug caught: a validator that refuses everything
    for the wrong reason (the message is asserted, not just the raise)."""
    raw = _cfg()
    mutate(raw)
    with pytest.raises(ConfigError, match=needle):
        Config.model_validate(raw)


def test_text_config_wants_the_text_stage() -> None:
    """Bug caught: stages=() so the warm matcher attaches a text cfg to ANY
    pod with the same base model, or the U14 shape — a stage no pod advertises."""
    from kinoforge.cli._commands import _cfg_want_stages

    cfg = Config.model_validate(_cfg())
    assert cfg.capability_key().stages == ("text",)
    assert _cfg_want_stages(cfg) == ("text",)


def test_video_config_key_is_unchanged_by_the_text_branch() -> None:
    """Bug caught: the new branch appending 'text' when cfg.text is None."""
    raw = _cfg()
    raw.pop("text")
    raw["engine"]["diffusers"].pop("capability")
    cfg = Config.model_validate(raw)
    assert cfg.capability_key().stages == ()


def test_load_config_accepts_the_yaml_shape() -> None:
    """Bug caught: a shape that validates from a dict but not from YAML (e.g. a
    validator reading a raw key the YAML loader renames)."""
    cfg = load_config(
        "engine:\n"
        "  kind: diffusers\n"
        "  precision: bf16\n"
        "  diffusers:\n"
        "    capability:\n"
        "      supported_modes: [t2t, it2t]\n"
        "models:\n"
        "  - kind: base\n"
        "    ref: hf:HuggingFaceTB/SmolVLM-256M-Instruct\n"
        "    target: checkpoints\n"
        "text:\n"
        "  engine: transformers\n"
        "  system: Be terse.\n"
        "compute:\n"
        "  provider: fake\n"
        "  image: fake:latest\n"
    )
    assert cfg.text is not None and cfg.text.system == "Be terse."
