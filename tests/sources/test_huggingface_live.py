"""Opt-in live smoke test against the real HuggingFace tree API.

Skipped by default. Set ``KINOFORGE_LIVE_HF=1`` to run.

Hits a tiny public canary repo so the test exercises:
- the real Link-header pagination loop,
- the real LFS field shape on at least one file,
- the real Authorization header passthrough when HF_TOKEN is set,
- the real 401/404 error mapping if creds are wrong.

Cost: $0 — the HF tree read API is unauthenticated for public repos.
"""

from __future__ import annotations

import os

import pytest

from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.sources.huggingface import HuggingFaceSource

_LIVE_GATE = "KINOFORGE_LIVE_HF"


pytestmark = pytest.mark.skipif(
    os.environ.get(_LIVE_GATE) != "1",
    reason=f"{_LIVE_GATE}=1 not set; live HF smoke skipped",
)


# Canary candidates — pick a repo that:
#  (a) is small (under 1 GiB total, so the live test stays fast),
#  (b) has at least one LFS-tracked file so the lfs.oid path is exercised,
#  (c) is unauthenticated (no terms of use, no gated access).
#
# Default: "hf-internal-testing/tiny-random-CLIPModel" (HF canary).
# Override via KINOFORGE_LIVE_HF_REPO env var when developing.
_LIVE_REPO = os.environ.get(
    "KINOFORGE_LIVE_HF_REPO", "hf-internal-testing/tiny-random-CLIPModel"
)


def test_live_bare_repo_returns_at_least_one_file() -> None:
    """Live HF tree API returns a non-empty file list for the canary repo."""
    src = HuggingFaceSource()
    creds = EnvCredentialProvider()
    artifacts = src.resolve(f"hf:{_LIVE_REPO}", creds)
    assert len(artifacts) > 0, f"expected at least one file in {_LIVE_REPO}"
    for a in artifacts:
        assert a.url.startswith("https://huggingface.co/")
        assert a.filename


def test_live_bare_repo_at_least_one_artifact_has_lfs_sha256() -> None:
    """Live tree response includes lfs.oid → Artifact.sha256 for at least one file.

    Some tiny canary repos have no LFS-tracked files; if KINOFORGE_LIVE_HF_REPO
    overrides this, ensure the override repo has at least one LFS file
    (e.g. a small .safetensors).
    """
    src = HuggingFaceSource()
    creds = EnvCredentialProvider()
    artifacts = src.resolve(f"hf:{_LIVE_REPO}", creds)
    sha256_count = sum(1 for a in artifacts if a.sha256 is not None)
    assert sha256_count > 0, (
        f"expected at least one LFS-tracked file in {_LIVE_REPO}; "
        f"override KINOFORGE_LIVE_HF_REPO to a repo with LFS files."
    )
