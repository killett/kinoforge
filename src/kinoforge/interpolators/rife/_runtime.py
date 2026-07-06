"""On-pod RIFE runtime — decode -> synthesize schedule -> mux at output fps.

Pod-side, standalone: given a local mp4 + a target fps, probes the source rate
and frame count, asks the engine-agnostic :func:`resolve_fps_target` for an
arbitrary-timestep schedule, synthesizes each ``(source_index, timestep)`` frame
via the RIFE model (``timestep == 0`` copies the source frame), and muxes the
result at the delivered rate. Mirrors the shape of the flashvsr runtime but is
far lighter — no diffsynth, no BSA.

Kept dependency-light for the pod embed: torch / the RIFE model are lazy-loaded
only when ``infer`` is not injected, and :func:`decimate_video_fps` is imported
lazily inside the (RIFE-never-hit) recursive-overshoot trim branch.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kinoforge.core.fps_resolver import InterpCapability, resolve_fps_target
from kinoforge.core.frames import ffprobe_fps

if TYPE_CHECKING:
    import numpy as np

# infer(frame_a, frame_b, timestep) -> synthesized frame. Frames are uint8
# HWC RGB ndarrays; timestep is in (0, 1). numpy stays a lazy runtime import
# for the pod embed — only the type-checker sees it.
InferFn = Callable[["np.ndarray", "np.ndarray", float], "np.ndarray"]


class RifeRuntime:
    """RIFE v4 arbitrary-timestep interpolator, pod-side.

    Attributes:
        weights_dir: Directory the provision step fetched RIFE weights into.
        model: RIFE model tag (e.g. ``"rife49"``).
        precision: ``"fp16"`` or ``"fp32"``.
    """

    def __init__(
        self,
        weights_dir: Path,
        model: str = "rife49",
        precision: str = "fp16",
        *,
        infer: InferFn | None = None,
    ) -> None:
        """Store config; defer model load until first ``interpolate`` call.

        Args:
            weights_dir: Directory holding the fetched RIFE weights.
            model: RIFE model tag.
            precision: Inference precision.
            infer: Optional injected synthesis seam ``(a, b, t) -> frame``;
                when ``None`` the real RIFE model is lazy-loaded on first use.
        """
        self.weights_dir = Path(weights_dir)
        self.model = model
        self.precision = precision
        self._infer = infer

    def _get_infer(self) -> InferFn:
        """Return the synthesis seam, lazy-loading the RIFE model if needed."""
        if self._infer is None:
            self._infer = self._load_rife_infer()
        return self._infer

    def _load_rife_infer(self) -> InferFn:
        """Load the pinned Practical-RIFE model and adapt it to ``InferFn``.

        Lazy — torch + the RIFE arch are only imported on a real pod run, never
        in the offline unit tests (which inject ``infer``).
        """
        import sys

        import numpy as np
        import torch

        # Practical-RIFE is a script repo (cloned by render_provision, not
        # pip-installed): its train_log/ package is only importable with the
        # repo root on sys.path. The arch + flownet.pkl were unzipped into
        # <repo>/train_log/ from the model release bundle.
        rife_repo = "/workspace/Practical-RIFE"
        train_log = rife_repo + "/train_log"
        if rife_repo not in sys.path:
            sys.path.insert(0, rife_repo)
        from train_log.RIFE_HDv3 import Model  # type: ignore[import-not-found]

        dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model = Model()
        # load_model reads ``{path}/flownet.pkl``; the arch-matched weights live
        # in train_log/ (NOT self.weights_dir, which is only a mirror).
        model.load_model(train_log, -1)
        model.eval()

        def _infer(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
            # RIFE v4's IFNet flow pyramid needs H/W padded up to a multiple of
            # 64 (480 -> 512), else the merge concat mismatches ("Expected size
            # 512 but got 480", 2026-07-05). Pad, infer, crop back.
            h, w = int(a.shape[0]), int(a.shape[1])
            ph = ((h - 1) // 64 + 1) * 64
            pw = ((w - 1) // 64 + 1) * 64
            pad = (0, pw - w, 0, ph - h)

            def _to_tensor(frame: np.ndarray) -> Any:  # noqa: ANN401 — torch.Tensor (untyped)
                arr = np.asarray(frame).astype(np.float32) / 255.0
                ten = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).to(dev)
                return torch.nn.functional.pad(ten, pad)

            with torch.no_grad():
                mid = model.inference(_to_tensor(a), _to_tensor(b), t)
            cropped = mid[0][:, :h, :w]
            out: np.ndarray = (
                (cropped.clamp(0, 1) * 255.0).byte().permute(1, 2, 0).cpu().numpy()
            )
            return out

        return _infer

    def interpolate(
        self, local_path: str | Path, target_fps: float, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Interpolate ``local_path`` to ``target_fps``; return a result dict.

        Args:
            local_path: Source mp4 on the pod.
            target_fps: Requested output frame rate.
            params: Engine-specific overrides (reserved; unused in v1).

        Returns:
            ``{filename, sha256, size, input_fps, output_fps,
            input_frame_count, output_frame_count, engine_meta}`` — the payload
            the server publishes as the job result.
        """
        del params  # reserved for future per-request knobs
        import imageio.v3 as iio
        import numpy as np

        src = Path(local_path)
        frames = np.stack(list(iio.imiter(str(src), plugin="FFMPEG")))
        source_fps = ffprobe_fps(src)
        count = int(frames.shape[0])

        plan = resolve_fps_target(
            source_fps,
            target_fps,
            InterpCapability.ARBITRARY_TIMESTEP,
            source_frame_count=count,
        )

        if plan.schedule is None:
            # Passthrough (equal fps) or pure decimation: copy every frame, then
            # decimate to the exact target only if the resolver asked for it
            # (RIFE never does — that path serves recursive-2x engines).
            out_frames = frames
            output_fps = (
                plan.decimate_to if plan.decimate_to is not None else source_fps
            )
        else:
            infer = self._get_infer()
            last = count - 1
            synthesized: list[Any] = []
            for i, t in plan.schedule:
                if t == 0.0:
                    synthesized.append(frames[i])
                else:
                    nxt = frames[min(i + 1, last)]
                    synthesized.append(infer(frames[i], nxt, t))
            out_frames = np.stack(synthesized).astype(np.uint8)
            output_fps = target_fps

        out_path = src.parent / f"{src.stem}_interp_{int(round(output_fps))}fps.mp4"
        self._mux(out_frames, output_fps, out_path)

        if plan.decimate_to is not None:
            from kinoforge.pipeline.decimate import decimate_video_fps

            trimmed = decimate_video_fps(out_path.read_bytes(), plan.decimate_to)
            out_path.write_bytes(trimmed)
            output_fps = plan.decimate_to

        output_frame_count = self._probe_frame_count(out_path)
        return {
            "filename": out_path.name,
            "sha256": _sha256_file(out_path),
            "size": out_path.stat().st_size,
            "input_fps": source_fps,
            "output_fps": output_fps,
            "input_frame_count": count,
            "output_frame_count": output_frame_count,
            "engine_meta": {"model": self.model, "precision": self.precision},
        }

    @staticmethod
    def _mux(frames: np.ndarray, fps: float, path: Path) -> None:
        """Encode ``frames`` (T, H, W, 3 uint8) to an H.264 mp4 at ``fps``."""
        from kinoforge.engines.diffusers.servers._video_io import write_mp4

        write_mp4(frames, int(round(fps)), path)

    @staticmethod
    def _probe_frame_count(path: Path) -> int:
        """Count video frames in ``path`` via ffprobe ``nb_read_packets``."""
        argv = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_packets",
            "-show_entries",
            "stream=nb_read_packets",
            "-of",
            "csv=p=0",
            str(path),
        ]
        out = subprocess.run(argv, capture_output=True, check=True)  # noqa: S603
        return int(out.stdout.decode().strip())


def _sha256_file(p: Path) -> str:
    """Stream-hash ``p`` with sha256."""
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
