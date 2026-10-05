"""Pure helpers behind `kinoforge text` (design §2.1, §2.2, §4.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.config import Config
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import Artifact
from kinoforge.core.text_request import (
    base_model_ref,
    build_request,
    declared_modes,
    derive_mode,
    image_arg_error,
    preflight_mode_error,
    resolve_prompt,
)


def _cfg(
    modes: list[str], *, text_prompt: str | None = None, top_prompt: str | None = None
) -> Config:
    raw: dict[str, Any] = {
        "engine": {
            "kind": "diffusers",
            "precision": "bf16",
            "diffusers": {"capability": {"supported_modes": modes}},
        },
        "models": [
            {"kind": "base", "ref": "hf:Qwen/Qwen3-0.6B", "target": "checkpoints"}
        ],
        "text": {"engine": "transformers"},
        "compute": {"provider": "fake", "image": "fake:latest"},
    }
    if text_prompt is not None:
        raw["text"]["prompt"] = text_prompt
    if top_prompt is not None:
        raw["prompt"] = top_prompt
    return Config.model_validate(raw)


def test_mode_is_derived_from_the_image_count() -> None:
    """Bug caught: a typed --mode leaking back in, or it2t for zero images."""
    assert derive_mode(0) == "t2t"
    assert derive_mode(1) == "it2t"
    assert derive_mode(3) == "it2t"


@pytest.mark.parametrize(
    ("cli", "block", "top", "expected"),
    [
        ("cli", "block", "top", "cli"),
        (None, "block", "top", "block"),
        (None, None, "top", "top"),
        ("   ", None, "top", "top"),
    ],
    ids=["cli-wins", "block-over-top", "top-alone", "whitespace-cli-is-absent"],
)
def test_prompt_precedence(
    cli: str | None, block: str | None, top: str | None, expected: str
) -> None:
    """Bug caught: config winning over the CLI, or '   ' accepted as a prompt."""
    assert (
        resolve_prompt(_cfg(["t2t"], text_prompt=block, top_prompt=top), cli)
        == expected
    )


def test_all_prompt_sources_absent_is_refused_naming_all_three() -> None:
    """Bug caught: a KeyError or a message that names only one of the fixes."""
    with pytest.raises(ValidationError, match=r"--prompt.*text\.prompt.*prompt:"):
        resolve_prompt(_cfg(["t2t"]), None)


def test_images_against_a_text_only_model_are_refused_pre_spend() -> None:
    """Bug caught: the gate reading the pod instead of the declaration, or a
    message that does not say which model and which modes."""
    err = preflight_mode_error(_cfg(["t2t"]), "it2t")
    assert err is not None
    assert "hf:Qwen/Qwen3-0.6B" in err
    assert "['t2t']" in err
    assert "it2t" in err
    assert preflight_mode_error(_cfg(["t2t", "it2t"]), "it2t") is None
    assert preflight_mode_error(_cfg(["t2t"]), "t2t") is None


def test_image_arg_errors_name_the_fault(tmp_path: Path) -> None:
    """Bug caught: a missing file passing the suffix check and failing on the pod."""
    good = tmp_path / "a.png"
    good.write_bytes(b"\x89PNG")
    assert image_arg_error(str(good)) is None
    assert "empty" in (image_arg_error("") or "")
    assert ".png/.jpg/.jpeg" in (image_arg_error(str(tmp_path / "a.gif")) or "")
    assert "not found" in (image_arg_error(str(tmp_path / "missing.png")) or "")


def test_image_arg_errors_refuse_urls() -> None:
    """Bug caught: a URL being accepted and failing on the pod."""
    assert "not supported" in (image_arg_error("http://example.com/a.png") or "")
    assert "not supported" in (image_arg_error("https://example.com/a.png") or "")


def test_build_request_numbers_roles_in_flag_order() -> None:
    """Bug caught: a shared role name (duplicate-role refusal downstream) or
    sorted-by-path order."""
    arts = [
        Artifact(uri="file:///z.png", sha256="z"),
        Artifact(uri="file:///a.jpg", sha256="a"),
    ]
    req = build_request("describe", arts)
    assert req.mode == "it2t"
    assert [a.role for a in req.assets] == ["image_1", "image_2"]
    assert [a.ref.uri for a in req.assets] == ["file:///z.png", "file:///a.jpg"]
    assert {a.kind for a in req.assets} == {"image"}
    assert build_request("hi", []).mode == "t2t"


def test_dict_lookups_match_the_config_lookups() -> None:
    """Bug caught: the engine/stage (which see cfg dicts) disagreeing with the
    CLI (which sees Config) about the model or the declared modes."""
    cfg = _cfg(["t2t", "it2t"])
    raw = cfg.model_dump(mode="json")
    assert base_model_ref(raw) == "hf:Qwen/Qwen3-0.6B"
    assert declared_modes(raw) == frozenset({"t2t", "it2t"})
    assert declared_modes(cfg) == frozenset({"t2t", "it2t"})
    with pytest.raises(ValidationError, match="kind: base"):
        base_model_ref({"models": []})
