"""FlashVSREngineConfig + UpscaleConfig integration + capability_key population.

Follows the Pydantic BaseModel pattern of SpandrelEngineConfig / UpscaleConfig.
Custom validators raise ConfigError (KinoforgeError subclass, NOT ValueError),
which Pydantic v2 propagates unwrapped — matching the plan's acceptance
criteria that all rejections surface as ConfigError.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import (
    BSACompileFailed,
    ConfigError,
    FlashVSRWeightsIncomplete,
    UnsupportedGpuArch,
)


def _full_cfg_with_upscale(upscale_block: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal-valid Config dict with the given upscale block.

    Multi-stage shape: real base model + upscale block. Avoids the
    upscale-only branch, which is orthogonal to the capability_key
    precision-thread we're covering here.
    """
    return {
        "engine": {"kind": "diffusers", "precision": "fp16"},
        "models": [
            {
                "ref": "hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers",
                "kind": "base",
                "target": "diffusion_models",
            },
        ],
        "compute": {
            "provider": "local",
            "image": "fake:latest",
            "lifecycle": {"budget": 1.0},
        },
        "upscale": upscale_block,
    }


def test_new_exceptions_importable_and_subclass_kinoforge_error() -> None:
    """RED: import path exists.

    Bug caught: forgetting to add the exception → downstream `except
    BSACompileFailed:` silently catches nothing.
    """
    from kinoforge.core.errors import KinoforgeError

    assert issubclass(BSACompileFailed, KinoforgeError)
    assert issubclass(FlashVSRWeightsIncomplete, KinoforgeError)
    assert issubclass(UnsupportedGpuArch, KinoforgeError)


def test_new_exceptions_carry_context() -> None:
    """RED: exceptions record post-mortem context, not just a string.

    Bug caught: raising `BSACompileFailed()` with no args → post-mortem
    can't tell which pod / stderr tail triggered it.
    """
    e1 = BSACompileFailed(pod_id="pod-xyz", stderr_tail="nvcc: OOM")
    assert e1.pod_id == "pod-xyz"
    assert "nvcc: OOM" in e1.stderr_tail

    e2 = FlashVSRWeightsIncomplete(
        filename="TCDecoder.ckpt", got_sha256="a" * 64, want_sha256="d" * 64
    )
    assert e2.filename == "TCDecoder.ckpt"
    assert e2.got_sha256 == "a" * 64
    assert e2.want_sha256 == "d" * 64

    e3 = UnsupportedGpuArch(got=(7, 5), required_major=8)
    assert e3.got == (7, 5)
    assert e3.required_major == 8


def test_flashvsr_config_valid_defaults() -> None:
    """RED: happy-path construction with all defaults."""
    from kinoforge.core.config import FlashVSREngineConfig

    c = FlashVSREngineConfig(weights_bundle="hf:JunhaoZhuang/FlashVSR-v1.1")
    assert c.precision == "bfloat16"
    assert c.window_size == 24
    assert c.tile_size == 0
    assert c.long_video_mode is False


@pytest.mark.parametrize("bad_precision", ["bf16", "int8", "FP16", "BFloat16", ""])
def test_flashvsr_config_rejects_bad_precision(bad_precision: str) -> None:
    """RED: precision allowlist enforced at cfg-time.

    Bug caught: typo `precision: bf16` silently accepted → runtime OOM
    on a dtype that maps to fp32 headers.
    """
    from kinoforge.core.config import FlashVSREngineConfig

    with pytest.raises(ConfigError, match="precision"):
        FlashVSREngineConfig(
            weights_bundle="hf:JunhaoZhuang/FlashVSR-v1.1",
            precision=bad_precision,
        )


def test_flashvsr_config_accepts_bfloat16() -> None:
    """RED: precision='bfloat16' is a first-class value (upstream default).

    Bug caught: `bfloat16` missing from allowlist → users copy the upstream
    YAML example verbatim and immediately hit a ConfigError at cfg-load, before
    the pod is even started.
    """
    from kinoforge.core.config import FlashVSREngineConfig

    c = FlashVSREngineConfig(weights_bundle="hf:x", precision="bfloat16")
    assert c.precision == "bfloat16"


def test_flashvsr_config_accepts_fp16_as_legacy() -> None:
    """RED: precision='fp16' still accepted for legacy DMD path.

    Bug caught: overly-narrow migration removes fp16 from the allowlist →
    any user YAML that explicitly sets `precision: fp16` breaks on upgrade.
    """
    from kinoforge.core.config import FlashVSREngineConfig

    c = FlashVSREngineConfig(weights_bundle="hf:x", precision="fp16")
    assert c.precision == "fp16"


def test_upscale_config_flashvsr_rejects_non4x_factor() -> None:
    """RED: scale='2x' with engine=flashvsr fails at cfg-load (native 4x lock).

    Bug caught: deferring the 4x check to runtime means a 2x cfg passes
    validation, the pod cold-boots (~10 min, ~$0.20), and then blows up
    with a weight-shape mismatch inside Causal_LQ4x_Proj.
    """
    from kinoforge.core.config import FlashVSREngineConfig, UpscaleConfig

    with pytest.raises(ConfigError, match="4x"):
        UpscaleConfig(
            engine="flashvsr",
            scale="2x",
            flashvsr=FlashVSREngineConfig(
                weights_bundle="hf:JunhaoZhuang/FlashVSR-v1.1"
            ),
        )


def test_upscale_config_flashvsr_accepts_4x_factor() -> None:
    """RED: scale='4x' with engine=flashvsr passes cfg-load.

    Bug caught: over-eager validator also rejects the one valid factor →
    no FlashVSR config can ever be constructed.
    """
    from kinoforge.core.config import FlashVSREngineConfig, UpscaleConfig

    cfg = UpscaleConfig(
        engine="flashvsr",
        scale="4x",
        flashvsr=FlashVSREngineConfig(weights_bundle="hf:JunhaoZhuang/FlashVSR-v1.1"),
    )
    assert cfg.scale == "4x"


@pytest.mark.parametrize("bad_window", [0, 7, 65, 128, -1])
def test_flashvsr_config_rejects_bad_window_size(bad_window: int) -> None:
    """RED: window_size clamp is HARD (raises) not soft (clamps silently).

    Bug caught: silent clamp masks a cfg typo (`window_size: 240` intended
    for a large-window path) → subtle quality regression.
    """
    from kinoforge.core.config import FlashVSREngineConfig

    with pytest.raises(ConfigError, match="window_size"):
        FlashVSREngineConfig(
            weights_bundle="hf:JunhaoZhuang/FlashVSR-v1.1",
            window_size=bad_window,
        )


@pytest.mark.parametrize("bad_tile", [1, 100, 513, 1024])
def test_flashvsr_config_rejects_off_allowlist_tile_size(bad_tile: int) -> None:
    """RED: tile_size allowlist enforced.

    Bug caught: `tile_size: 100` silently accepted → BSA hits a shape
    misaligned with its block size and either crashes at runtime or
    produces subtle border artifacts.
    """
    from kinoforge.core.config import FlashVSREngineConfig

    with pytest.raises(ConfigError, match="tile_size"):
        FlashVSREngineConfig(
            weights_bundle="hf:JunhaoZhuang/FlashVSR-v1.1",
            tile_size=bad_tile,
        )


def test_flashvsr_config_rejects_unknown_weights_scheme() -> None:
    """RED: unknown scheme fails at cfg-time, not resolver-time.

    Bug caught: `weights_bundle: gs://bucket/...` silently accepted →
    pod cold-boot burns 60s before failing on the resolver.
    """
    from kinoforge.core.config import FlashVSREngineConfig

    with pytest.raises(ConfigError, match="weights_bundle"):
        FlashVSREngineConfig(weights_bundle="gs://bucket/flashvsr")


def test_upscale_config_flashvsr_engine_requires_block() -> None:
    """RED: `engine: flashvsr` with no `flashvsr:` block fails cfg-time.

    Bug caught: forgotten block → pod cold-boot then late runtime error
    about missing weights_bundle.
    """
    from kinoforge.core.config import UpscaleConfig

    with pytest.raises(ConfigError, match="flashvsr"):
        UpscaleConfig(engine="flashvsr", scale="2x", flashvsr=None)


def test_upscale_config_flashvsr_rejects_height_scale() -> None:
    """RED: height-target refused at cfg-time (spec §4.2 fail-fast).

    Bug caught: deferring the reject to runtime burns the pod cold-boot.
    """
    from kinoforge.core.config import FlashVSREngineConfig, UpscaleConfig

    with pytest.raises(ConfigError, match="height-target"):
        UpscaleConfig(
            engine="flashvsr",
            scale="1080p",
            flashvsr=FlashVSREngineConfig(
                weights_bundle="hf:JunhaoZhuang/FlashVSR-v1.1"
            ),
        )


def test_capability_key_populates_flashvsr_precision() -> None:
    """RED: capability_key threads flashvsr precision into the key.

    Bug caught: forgetting to extend `capability_key()` → an fp32
    FlashVSR request lands on an fp16-warm pod without triggering a
    reload; wrong-dtype inference.
    """
    from kinoforge.core.config import Config

    cfg = Config.model_validate(
        _full_cfg_with_upscale(
            {
                "engine": "flashvsr",
                "scale": "4x",
                "flashvsr": {
                    "weights_bundle": "hf:JunhaoZhuang/FlashVSR-v1.1",
                    "precision": "fp32",
                },
            }
        )
    )
    key = cfg.capability_key()
    assert key.upscaler == "flashvsr"
    assert key.upscaler_precision == "fp32"


# T7.5.e — BSA wheel-URL cfg surface (source-compile → prebuilt-wheel swap).


def test_flashvsr_config_bsa_wheel_url_defaults_to_pinned_gh_release_url() -> None:
    """RED: default bsa_wheel_url points at killett/kinoforge-artifacts GH release.

    Bug caught: default blanked, drifted to a different host, or accidentally
    reverted to `git+https://github.com/mit-han-lab/Block-Sparse-Attention` —
    every FlashVSR pod cold-boot regresses to the 30-min BSA source compile
    (root cause of the T7.5 spend). Release-tag substring guard catches a
    silent CUDA/torch ABI swap under the same URL.
    """
    from urllib.parse import urlparse

    from kinoforge.core.config import FlashVSREngineConfig

    c = FlashVSREngineConfig(weights_bundle="hf:JunhaoZhuang/FlashVSR-v1.1")

    parsed = urlparse(c.bsa_wheel_url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "github.com"
    assert parsed.path.startswith("/killett/kinoforge-artifacts/releases/download/")
    assert parsed.path.endswith(".whl")
    assert "bsa-cu128-torch2.8" in parsed.path, (
        "release tag must encode CUDA + torch pin so a future rebuild "
        "cannot silently ship a mismatched-ABI wheel under the same URL"
    )


@pytest.mark.parametrize(
    "bad_url",
    [
        "git+https://github.com/mit-han-lab/Block-Sparse-Attention@3453bbb1",
        "file:///tmp/bsa.whl",
        "",
        "bare-string-no-scheme",
        "ssh://user@host/path.whl",
        "s3://bucket/bsa.whl",
        "gs://bucket/bsa.whl",
    ],
)
def test_flashvsr_config_rejects_bad_bsa_wheel_url_scheme(bad_url: str) -> None:
    """RED: unknown / non-fetchable schemes fail cfg-time.

    Bug caught: `bsa_wheel_url: git+https://...` silently accepted → provision
    script `curl`s the git URL, gets HTML back, `pip install` chokes with a
    cryptic error 25 min into the cold-boot instead of failing at cfg-load.
    """
    from kinoforge.core.config import FlashVSREngineConfig

    with pytest.raises(ConfigError, match="bsa_wheel_url"):
        FlashVSREngineConfig(
            weights_bundle="hf:JunhaoZhuang/FlashVSR-v1.1",
            bsa_wheel_url=bad_url,
        )


@pytest.mark.parametrize(
    "good_url",
    [
        "https://huggingface.co/emmykillett/kinoforge-artifacts/resolve/main/bsa.whl",
        "http://internal.example/bsa.whl",
        "hf:emmykillett/kinoforge-artifacts/bsa.whl",
    ],
)
def test_flashvsr_config_accepts_valid_bsa_wheel_url_schemes(good_url: str) -> None:
    """RED: allowlist schemes (https, http, hf:) round-trip cleanly.

    Bug caught: overly-tight validator (e.g., regex pinned to
    `^https://huggingface\\.co/`) rejects a CI-side fallback URL or a
    GitHub-release asset URL — kills the fallback path.
    """
    from kinoforge.core.config import FlashVSREngineConfig

    c = FlashVSREngineConfig(
        weights_bundle="hf:JunhaoZhuang/FlashVSR-v1.1",
        bsa_wheel_url=good_url,
    )
    assert c.bsa_wheel_url == good_url
