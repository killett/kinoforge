r"""CLI — fetch FlashVSR v1.1 weights bundle, verify SHA256 against manifest.

Invoked in the pod bootstrap:

    python -m kinoforge.upscalers.flashvsr._fetch_weights \
        --bundle hf:JunhaoZhuang/FlashVSR-v1.1 \
        --dest /workspace/models/flashvsr \
        --include-long-video 0

Pod-safe: does NOT import ``kinoforge.core.registry`` / interfaces / adapters
— runs with only ``kinoforge.upscalers.flashvsr`` + ``kinoforge.core.errors``
embedded (mirrors the P2 spandrel _fetch_weights lesson that busted the
64 KB RunPod env-var ceiling).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import urllib.request
from importlib import resources
from pathlib import Path

# kinoforge.core.errors is lazy-imported inside _verify() — a top-level
# import triggers kinoforge/core/__init__.py which pulls the registry
# (via splitter self-registration), which is the exact bloat the pod-safe
# embed shape MUST NOT drag along (see P2 64 KB env-var ceiling incident).

_BASE_FILES = (
    "diffusion_pytorch_model_streaming_dmd.safetensors",
    "Wan2.1_VAE.pth",
)
_LONG_VIDEO_FILES = ("LQ_proj_in.ckpt", "TCDecoder.ckpt")

_HF_REF_RE = re.compile(r"^hf:([^/]+/[^/]+)$")
_HF_BASE = "https://huggingface.co"


def _load_manifest() -> dict[str, dict[str, str]]:
    """Read the shipped ``weights_manifest.json``."""
    with (
        resources.files("kinoforge.upscalers.flashvsr")
        .joinpath("weights_manifest.json")
        .open("r") as f
    ):
        data: dict[str, dict[str, str]] = json.load(f)
        return data


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_one(bundle_ref: str, filename: str, dest_dir: Path) -> Path:
    """Fetch one file from the bundle ref into ``dest_dir``; return the path.

    Test seam — patched to a deterministic stub in unit tests.
    """
    if bundle_ref.startswith("hf:"):
        m = _HF_REF_RE.match(bundle_ref)
        if m is None:
            raise ValueError(f"malformed hf bundle ref: {bundle_ref!r}")
        repo = m.group(1)
        url = f"{_HF_BASE}/{repo}/resolve/main/{filename}"
        token = os.environ.get("HF_TOKEN")
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"} if token else {}
    elif bundle_ref.startswith(("http://", "https://")):
        url = bundle_ref.rstrip("/") + "/" + filename
        headers = {}
    else:
        raise ValueError(
            f"unsupported bundle scheme: {bundle_ref!r} (supported: hf:, http(s)://)"
        )

    target = dest_dir / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    headers.setdefault("User-Agent", "kinoforge-flashvsr-fetch/0.1")
    req = urllib.request.Request(url, headers=headers)  # noqa: S310
    tmp = target.with_suffix(target.suffix + ".partial")
    try:
        with (
            urllib.request.urlopen(req, timeout=600) as resp,  # noqa: S310
            tmp.open("wb") as out,
        ):
            shutil.copyfileobj(resp, out)
        tmp.replace(target)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
    return target


def _verify(path: Path, expected_sha: str) -> None:
    got = _sha256(path)
    if got != expected_sha:
        # Lazy import — see module-level comment on pod-safe embed shape.
        from kinoforge.core.errors import FlashVSRWeightsIncomplete

        raise FlashVSRWeightsIncomplete(
            filename=path.name, got_sha256=got, want_sha256=expected_sha
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kinoforge.upscalers.flashvsr._fetch_weights")
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--include-long-video", required=True, choices=("0", "1"))
    args = parser.parse_args(argv)

    files = list(_BASE_FILES)
    if args.include_long_video == "1":
        files += list(_LONG_VIDEO_FILES)

    args.dest.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest()
    for name in files:
        path = _download_one(args.bundle, name, args.dest)
        _verify(path, manifest[name]["sha256"])
        print(f"wrote {path} sha256={manifest[name]['sha256'][:8]}")
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    raise SystemExit(main())
