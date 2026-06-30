r"""Materialise SeedVR2 weights via kinoforge's source-resolver path.

CLI entry point invoked by the pod's provision script.

Usage::

    python -m kinoforge.upscalers.seedvr2._fetch_weights \
        --variant 3B --precision fp8 --dest /workspace/models/seedvr2

Args validated against the (variant, precision) matrix the engine supports.
Dispatches through ``kinoforge.core.registry.source_for_ref`` so HuggingFace
auth / caching / retry are all inherited from the existing path.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from kinoforge.core import registry
from kinoforge.core.credentials import EnvCredentialProvider


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kinoforge.upscalers.seedvr2._fetch_weights")
    p.add_argument("--variant", choices=["3B", "7B"], required=True)
    p.add_argument("--precision", choices=["fp8", "fp16"], required=True)
    p.add_argument("--dest", type=Path, required=True)
    return p


def _ref_for(variant: str) -> str:
    return f"hf:ByteDance-Seed/SeedVR2-{variant}"


def main(argv: list[str] | None = None) -> int:
    """Resolve the SeedVR2 weights via the kinoforge source registry.

    Args:
        argv: Optional argv slice (used by tests); ``None`` means
            ``sys.argv[1:]``.

    Returns:
        Exit code (0 on success).
    """
    args = _build_parser().parse_args(argv)
    ref = _ref_for(args.variant)
    source = registry.source_for_ref(ref)
    artifacts = source.resolve(ref, EnvCredentialProvider())
    for art in artifacts:
        print(f"resolved {ref} -> {art.uri}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
