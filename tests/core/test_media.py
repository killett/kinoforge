"""The media convention: one definition of "video" | "image" and its default."""

from __future__ import annotations

from pathlib import Path

import pytest

import kinoforge._adapters  # noqa: F401 — self-register every upscaler
from kinoforge.core import registry
from kinoforge.core.interfaces import (
    Artifact,
    UpscaleJob,
    UpscalerEngine,
    UpscaleResult,
)
from kinoforge.core.scale_target import ScaleTarget


class TestMediaOf:
    def test_absent_meta_means_video(self) -> None:
        # Bug caught: a default of None/"" leaks into extension_for and the
        # orchestrator publishes an upscaled clip with no extension.
        from kinoforge.core.media import media_of

        assert media_of(Artifact(uri="file:///x.mp4")) == "video"

    def test_image_meta_is_read(self) -> None:
        # Bug caught: media_of ignores the meta and always returns "video",
        # so every --image run publishes as .mp4.
        from kinoforge.core.media import media_of

        art = Artifact(uri="file:///x.png", meta={"media": "image"})
        assert media_of(art) == "image"

    def test_garbage_meta_raises(self) -> None:
        # Bug caught: a typo'd meta value silently falls back to video and
        # a PNG is published as .mp4 — the exact defect this module exists
        # to make impossible.
        from kinoforge.core.media import media_of

        with pytest.raises(ValueError, match="media"):
            media_of(Artifact(uri="file:///x", meta={"media": "still"}))


class TestExtensionFor:
    def test_mapping(self) -> None:
        # Bug caught: the two extensions swapped or extension_for returning
        # a bare suffix without the dot, so published filenames are wrong.
        from kinoforge.core.media import extension_for

        assert extension_for("image") == ".png"
        assert extension_for("video") == ".mp4"


class TestUpscaleJobMedia:
    def test_default_is_video(self) -> None:
        # Bug caught: a REQUIRED media field breaks every existing
        # UpscaleJob(...) construction in src/ and tests/.
        job = UpscaleJob(
            source=Artifact(uri="file:///x.mp4"),
            scale=ScaleTarget(kind="factor", value=2.0),
        )
        assert job.media == "video"

    def test_image_is_accepted(self) -> None:
        # Bug caught: a Literal typo or a validator rejecting "image",
        # so the stage can never build an image job.
        job = UpscaleJob(
            source=Artifact(uri="file:///x.png"),
            scale=ScaleTarget(kind="factor", value=2.0),
            media="image",
        )
        assert job.media == "image"


class _Bare(UpscalerEngine):
    name = "_bare"
    requires_compute = False
    requires_local_weights = False
    supported_scales = ()

    def provision(
        self, instance: object, cfg: object, *, cancel_token: object = None
    ) -> None:
        return None

    def upscale(
        self, instance: object, job: object, cfg: object, *, cancel_token: object = None
    ) -> UpscaleResult:
        raise NotImplementedError

    def validate_spec(self, job: object) -> None:
        return None

    def model_identity(self, cfg: object) -> str:
        return "_bare"


class TestSupportsImageInput:
    def test_default_is_false(self) -> None:
        # Bug caught: a future engine inherits "yes" by accident and the CLI
        # boots a 10-minute pod for an engine that cannot read a PNG.
        assert _Bare().supports_image_input is False

    def test_only_spandrel_declares_support(self) -> None:
        names = registry.upscaler_names()
        # Guard the guard: a sweep over nothing passes everything.
        assert len(names) >= 3, names
        supporting: set[str] = set()
        for name in names:
            factory = registry.get_upscaler(name)
            if getattr(factory, "supports_image_input", False):
                supporting.add(name)
        assert supporting == {"spandrel"}


class TestDirImageSuffixes:
    def test_every_suffix_has_a_pillow_opener(self) -> None:
        # Bug caught: a typo'd suffix (".jpge") that no file ever matches,
        # or a suffix Pillow cannot open, silently never reaches the pod.
        import pillow_heif
        from PIL import Image

        from kinoforge.core.media import DIR_IMAGE_SUFFIXES

        pillow_heif.register_heif_opener()
        registered = Image.registered_extensions()
        openable = {ext for ext, fmt in registered.items() if fmt in Image.OPEN}
        missing = sorted(s for s in DIR_IMAGE_SUFFIXES if s not in openable)
        assert missing == []
        # Guard the guard: a sweep over an empty set passes everything.
        assert len(DIR_IMAGE_SUFFIXES) >= 20

    def test_non_image_suffixes_are_excluded(self) -> None:
        # Bug caught: "everything Pillow registers" would classify .mpg,
        # .pdf and .h5 as images and feed them to the planner.
        from kinoforge.core.media import DIR_IMAGE_SUFFIXES

        for bad in (".mpg", ".mpeg", ".pdf", ".ps", ".eps", ".h5", ".hdf", ".bufr"):
            assert bad not in DIR_IMAGE_SUFFIXES

    def test_pod_suffixes_are_a_subset(self) -> None:
        # Bug caught: a PNG in the directory counted as "non-image".
        from kinoforge.core.media import DIR_IMAGE_SUFFIXES, IMAGE_SUFFIXES

        assert IMAGE_SUFFIXES <= DIR_IMAGE_SUFFIXES
        assert all(s == s.lower() and s.startswith(".") for s in DIR_IMAGE_SUFFIXES)


class TestLocalArtifact:
    def test_stamps_uri_sha_size_and_media(self, tmp_path: Path) -> None:
        # Bug caught: a relative uri, a sha of the wrong bytes, or a missing
        # media stamp means the pod receives a "video".
        import hashlib

        from kinoforge.core.media import local_artifact

        p = tmp_path / "in.png"
        p.write_bytes(b"\x89PNG not really")
        art = local_artifact(p, "image")
        assert art.uri == f"file://{p.resolve()}"
        assert art.sha256 == hashlib.sha256(b"\x89PNG not really").hexdigest()
        assert art.size == p.stat().st_size
        assert art.meta == {"media": "image"}

    def test_cli_resolver_delegates(self, tmp_path: Path) -> None:
        # Bug caught: the CLI keeps its own copy and the two drift.
        from kinoforge.cli._commands import _resolve_input_as_artifact
        from kinoforge.core.media import local_artifact

        p = tmp_path / "in.mp4"
        p.write_bytes(b"mp4")
        assert _resolve_input_as_artifact(str(p), "video") == local_artifact(p, "video")
