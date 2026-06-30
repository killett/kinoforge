r"""CLI module — fetch spandrel SR weights via the kinoforge source-resolver chain.

Invoked by SpandrelEngine.render_provision on the pod's bootstrap:

    python -m kinoforge.upscalers.spandrel._fetch_weights \
        --url <ref> --dest /workspace/models/spandrel

Symmetric with ``src/kinoforge/upscalers/seedvr2/_fetch_weights.py`` — same
resolver dispatch via ``registry.source_for_ref``. Unlike the seedvr2
variant (which lets HF Hub's snapshot_download own caching), spandrel
weights must land on disk at ``--dest`` because ``SpandrelRuntime`` reads
the weights file path directly. Downloads each resolved artifact via the
URL + headers the source provides.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass
class _ResolvedRef:
    """Minimal Artifact-shape ref used by the on-pod _fetch_weights CLI.

    Mirrors :class:`kinoforge.core.interfaces.Artifact`'s download-facing
    surface (``url`` / ``filename`` / ``headers``) without depending on
    the full kinoforge package — this module runs on a pod whose
    ``embed_modules`` only includes ``kinoforge.upscalers.spandrel``, not
    the source-resolver chain or _adapters.
    """

    url: str
    filename: str
    headers: dict[str, str]


_HF_REF_RE = re.compile(r"^hf:([^/]+/[^/]+)(/(.+))?$")
_HF_BASE = "https://huggingface.co"


def _resolve_source(url: str) -> list[_ResolvedRef]:
    """Resolve a source ref to download targets.

    Supports the three schemes spandrel weights cfgs are expected to use:

    - ``hf:<repo>/<path>`` → HuggingFace ``resolve/main`` URL with
      ``Authorization: Bearer $HF_TOKEN`` when the env var is set.
    - ``http(s)://...`` → passthrough.
    - ``file://...`` → local file copy.

    Test seam — monkeypatch in unit tests to short-circuit network I/O.
    Standalone implementation (no ``kinoforge._adapters`` import) so the
    pod-side CLI can run with only ``kinoforge.upscalers.spandrel``
    embedded.
    """
    if url.startswith("hf:"):
        m = _HF_REF_RE.match(url)
        if m is None:
            raise ValueError(f"malformed hf ref: {url!r}")
        repo, _, path = m.groups()
        if not path:
            raise ValueError(
                f"hf ref {url!r} resolves a single file — supply <repo>/<file>"
            )
        filename = path.rsplit("/", 1)[-1]
        token = os.environ.get("HF_TOKEN")
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"} if token else {}
        return [
            _ResolvedRef(
                url=f"{_HF_BASE}/{repo}/resolve/main/{path}",
                filename=filename,
                headers=headers,
            )
        ]
    if url.startswith(("http://", "https://")):
        filename = url.rsplit("/", 1)[-1] or "weights.bin"
        return [_ResolvedRef(url=url, filename=filename, headers={})]
    if url.startswith("file://"):
        local = url.removeprefix("file://")
        filename = Path(local).name
        return [_ResolvedRef(url=url, filename=filename, headers={})]
    raise ValueError(f"unsupported url scheme: {url!r}")


def _download(artifact: _ResolvedRef, dest_dir: Path) -> Path:
    """Stream the artifact's bytes to ``dest_dir/<filename>`` and return the path."""
    target = dest_dir / artifact.filename
    target.parent.mkdir(parents=True, exist_ok=True)
    headers = dict(artifact.headers or {})
    headers.setdefault("User-Agent", "kinoforge-pod-fetch/0.1")
    req = urllib.request.Request(  # noqa: S310 — caller-resolved URL
        artifact.url, headers=headers
    )
    tmp = target.with_suffix(target.suffix + ".partial")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp, tmp.open("wb") as out:  # noqa: S310
            shutil.copyfileobj(resp, out)
        tmp.replace(target)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
    return target


def main(argv: list[str] | None = None) -> int:
    """Argparse entry point.

    Returns:
        Exit code (0 on success, 2 on argparse error via SystemExit, 1 on
        resolver / download error).
    """
    parser = argparse.ArgumentParser(prog="kinoforge.upscalers.spandrel._fetch_weights")
    parser.add_argument(
        "--url",
        required=True,
        help="source ref (hf:, civitai:, civarchive:, http(s)://)",
    )
    parser.add_argument(
        "--dest", required=True, type=Path, help="destination directory"
    )
    args = parser.parse_args(argv)

    try:
        artifacts = _resolve_source(args.url)
    except Exception as exc:  # noqa: BLE001 — surface to caller
        sys.stderr.write(f"error: resolve failed: {exc}\n")
        return 1

    if not artifacts:
        sys.stderr.write(f"error: no artifacts resolved for url: {args.url!r}\n")
        return 1

    args.dest.mkdir(parents=True, exist_ok=True)
    for art in artifacts:
        try:
            written = _download(art, args.dest)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"error: download failed: {exc}\n")
            return 1
        print(f"wrote {written}")

    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    raise SystemExit(main())
