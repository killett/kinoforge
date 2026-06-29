"""SeedVR2Runtime — thin wrapper around upstream ByteDance-Seed/SeedVR.

Upstream is NOT vendored. The provision script installs it from a pinned
commit SHA. This module's import is intentionally lazy: importing the
module does not import the upstream package — only constructing
:class:`SeedVR2Runtime` does. Keeps kinoforge importable on a host
without the upstream installed (e.g. unit tests on the dev box).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from kinoforge.core.errors import NotYetImplementedError
from kinoforge.core.scale_target import ScaleTarget


class SeedVR2Runtime:
    """Wraps upstream SeedVR2 inference. Held inside ``_LOADED[name].pipe`` on the server.

    Args:
        weights_dir: Local path to the downloaded SeedVR2 weights.
        variant: ``"3B"`` or ``"7B"``.
        precision: ``"fp8"`` or ``"fp16"``.

    Raises:
        ImportError: Upstream ``seedvr`` package not installed (caller responsibility).
    """

    def __init__(
        self,
        weights_dir: Path,
        variant: Literal["3B", "7B"],
        precision: Literal["fp8", "fp16"],
    ) -> None:
        from seedvr.inference import SeedVR2Inferencer  # type: ignore[import-not-found]

        self._inferencer: Any = SeedVR2Inferencer.from_pretrained(
            weights_dir, variant=variant, dtype=precision
        )
        self._variant = variant
        self._precision = precision

    def upscale(
        self, video_path: Path, scale: ScaleTarget, params: dict[str, Any]
    ) -> Path:
        """Run SeedVR2 inference on a single clip.

        Args:
            video_path: Local path to the input mp4.
            scale: ``ScaleTarget``. ``kind="height"`` raises
                :class:`NotYetImplementedError`.
            params: Engine-specific overrides (``tile_size``, ``steps``, ...).

        Returns:
            Local path to the upscaled mp4.

        Raises:
            NotYetImplementedError: ``ScaleTarget(kind="height")`` is v1-deferred.
        """
        if scale.kind == "height":
            raise NotYetImplementedError(
                f"height-target upscale (e.g. {int(scale.value)}p) deferred "
                f"to a later session; use --scale Nx for v1"
            )
        return Path(
            self._inferencer.upscale(
                video_path,
                factor=scale.value,
                **{k: v for k, v in params.items() if v is not None},
            )
        )

    def to(self, device: str) -> None:
        """LRU eviction hook — move underlying nn.Modules between devices.

        Args:
            device: ``"cuda"`` | ``"cpu"`` | ``"disk"``. The ``"disk"`` case
                is handled by the server deleting the runtime instance and
                reloading on next activation; this method only supports
                cuda/cpu moves.
        """
        self._inferencer.to(device)
