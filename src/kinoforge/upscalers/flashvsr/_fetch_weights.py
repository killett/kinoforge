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
import os
import re
import shutil
import urllib.request
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

# Manifest inlined as a Python constant (previously ``weights_manifest.json``
# alongside this module). Rationale: the pod bootstrap embeds only ``.py``
# files under kinoforge.upscalers.flashvsr — a JSON sibling gets silently
# dropped by ``_render_embed_lines`` (line 175 of
# ``src/kinoforge/engines/diffusers/__init__.py``: ``.endswith(".py")``
# filter). Runtime probe on 2026-07-02 (pod ``igerhjv1cx94pl``) crashed
# with ``FileNotFoundError: /tmp/kfsrv/kinoforge/upscalers/flashvsr/
# weights_manifest.json``. Inlining keeps the manifest coupled to the
# code that consumes it and eliminates the pod-embed drop entirely.
_MANIFEST: dict[str, dict[str, str]] = {
    "diffusion_pytorch_model_streaming_dmd.safetensors": {
        "sha256": "bd28180edcf3446c028e32fc6b731a80bf7e4da2ab4caac3186b9499964d37be",
    },
    "Wan2.1_VAE.pth": {
        "sha256": "38071ab59bd94681c686fa51d75a1968f64e470262043be31f7a094e442fd981",
    },
    "LQ_proj_in.ckpt": {
        "sha256": "d6d011cdaaba6a52645086caa08fa04124e746f6ca568140a24007591142bfd2",
    },
    "TCDecoder.ckpt": {
        "sha256": "e224bdcf2f52745cbf4d393ff5374c2ba09e90285d5d19062d2bf63b915b6161",
    },
}


def _load_manifest() -> dict[str, dict[str, str]]:
    """Return the FlashVSR v1.1 weights-file SHA256 manifest.

    Inlined-constant lookup (see ``_MANIFEST`` for rationale). Kept as a
    function to preserve the existing test-seam patch point and to allow
    a lazy switch back to on-disk loading if the manifest ever grows too
    large to co-locate with the code.
    """
    return dict(_MANIFEST)


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
