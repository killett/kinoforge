"""generate_image: the terminal image path (no pipeline, no compute)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from kinoforge.core.config import Config
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    ImageBackend,
    ImageEngine,
    ImageJob,
    ImageProfile,
    ImageProfileProvider,
    Instance,
)

T2I_PROFILE = ImageProfile(
    name="fake-image", max_resolution=(1024, 1024), supported_modes={"t2i"}
)
VIDEO_ONLY_PROFILE = ImageProfile(
    name="wrong", max_resolution=(1024, 1024), supported_modes={"t2v"}
)

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-pixels"


def _cfg(**image_over: Any) -> Config:
    block = {"engine": "fake", "spec": {"model": "m"}, **image_over}
    return Config.model_validate({"image": block})


@dataclass
class _SpyBackend(ImageBackend):
    """ImageBackend double recording whether submit was ever reached.

    Correction (brief said these doubles were plain dataclasses): a plain
    dataclass with no ``ImageBackend`` base is not a nominal subtype of
    ``ImageBackend``, so mypy rejects passing it to ``resolve_image_stack``'s
    ``image_engine``/backend-typed parameters. Subclassing the ABC (as the
    established doubles in ``test_image_interfaces.py`` already do) fixes
    this without loosening any assertion.
    """

    submits: list[ImageJob] = field(default_factory=list)

    def capabilities(self) -> ImageProfile:
        return T2I_PROFILE

    def inspect_capabilities(self) -> ImageProfile:
        return T2I_PROFILE

    def submit(self, job: ImageJob) -> str:
        self.submits.append(job)
        return "job-1"

    def result(self, job_id: str, *, cancel_token: object | None = None) -> Artifact:
        # Third correction: the brief's Artifact here carried neither `uri`
        # nor `url`, so `artifact_bytes()` (uri -> file, url -> http,
        # otherwise synthetic) always fell through to its synthetic-bytes
        # branch — `published["bytes"] == PNG` could never pass against the
        # literal fixture. An `url` makes the http(s) branch fire, which is
        # what actually exercises the injected `http_get_bytes` seam the
        # tests pass in.
        return Artifact(
            filename="out.png",
            url="https://fake-engine.example/out.png",
            meta={"_synthetic": True},
        )

    def endpoints(self) -> dict[str, str]:
        return {}


@dataclass
class _SpyEngine(ImageEngine):
    """ImageEngine double with a controllable model_identity.

    See the ``_SpyBackend`` docstring for why this subclasses ``ImageEngine``
    rather than standing alone as the brief originally had it.
    """

    name: str = "fake"
    requires_compute: bool = False
    requires_local_weights: bool = False
    identity: str = "fake-model"
    backend_obj: _SpyBackend = field(default_factory=_SpyBackend)

    def provision(
        self,
        instance: Instance | None,
        cfg: dict[str, object],
        *,
        cancel_token: object | None = None,
    ) -> None:
        return None

    def backend(self, instance: Instance | None, cfg: dict[str, object]) -> _SpyBackend:
        return self.backend_obj

    def profile_for(self, key: CapabilityKey) -> ImageProfile:
        return T2I_PROFILE

    def validate_spec(self, job: ImageJob) -> None:
        if not job.prompt:
            raise ValidationError("spy: prompt is empty")

    def model_identity(self, cfg: dict[str, object]) -> str:
        return self.identity


@dataclass
class _StubProvider(ImageProfileProvider):
    """ImageProfileProvider double; same subclassing correction as above.

    Second correction: ``profile: ImageProfile = T2I_PROFILE`` (the brief's
    literal text) is a mutable default on a dataclass field. ``ImageProfile``
    is ``@dataclass`` without ``frozen=True``, so it is unhashable, and
    Python's dataclass machinery rejects any unhashable default outright —
    this raises ``ValueError: mutable default <class 'ImageProfile'> for
    field profile is not allowed: use default_factory`` at class-definition
    time, which would fail collection for the entire test module (confirmed
    empirically). ``default_factory`` returning the shared singleton (not a
    fresh copy) fixes it while keeping the same default value.
    """

    profile: ImageProfile = field(default_factory=lambda: T2I_PROFILE)

    def resolve(self, key: CapabilityKey) -> ImageProfile:
        return self.profile

    def discover(
        self, key: CapabilityKey, engine: ImageEngine, backend: ImageBackend
    ) -> ImageProfile:
        return self.profile

    def verify(
        self,
        profile: ImageProfile,
        backend: ImageBackend,
        *,
        engine: ImageEngine | None = None,
        key: CapabilityKey | None = None,
    ) -> None:
        return None


@dataclass
class _SpySink:
    published: list[dict[str, Any]] = field(default_factory=list)

    def publish(self, data: bytes, **kwargs: Any) -> str:
        self.published.append({"bytes": data, **kwargs})
        return f"/out/{kwargs.get('kind')}.png"


def _store(tmp_path: Any) -> Any:
    from kinoforge.stores.local import LocalArtifactStore

    return LocalArtifactStore(root=tmp_path)


def test_happy_path_stores_and_publishes(tmp_path: Any) -> None:
    """One prompt in, one stored PNG and one published PNG out.

    Bug this catches: publishing without storing (so `kinoforge gc` can never
    see the artifact) or storing without publishing (so the operator never
    gets a file).
    """
    from kinoforge.core.image_run import generate_image

    engine, sink = _SpyEngine(), _SpySink()
    artifact = generate_image(
        _cfg(prompt="a cat"),
        store=_store(tmp_path),
        run_id="image-20261003-120000",
        sink=sink,
        image_engine=engine,
        image_profile_provider=_StubProvider(),
        http_get_bytes=lambda url, headers: PNG,
    )
    assert artifact is not None
    assert len(sink.published) == 1
    published = sink.published[0]
    assert published["kind"] == "image"
    assert published["extension"] == ".png"
    assert published["provider"] == "fake"
    assert published["model"] == "fake-model"
    assert published["bytes"] == PNG


def test_sink_none_stores_only(tmp_path: Any) -> None:
    """--no-output-dir must still produce a stored artifact.

    Bug this catches: an unguarded self.sink.publish, which would make
    --no-output-dir crash with AttributeError on None.
    """
    from kinoforge.core.image_run import generate_image

    artifact = generate_image(
        _cfg(prompt="a cat"),
        store=_store(tmp_path),
        run_id="image-1",
        sink=None,
        image_engine=_SpyEngine(),
        image_profile_provider=_StubProvider(),
        http_get_bytes=lambda url, headers: PNG,
    )
    assert artifact is not None


@pytest.mark.parametrize(
    ("override", "block_prompt", "top_prompt", "expected"),
    [
        ("from-cli", "from-block", "from-top", "from-cli"),
        (None, "from-block", "from-top", "from-block"),
        (None, None, "from-top", "from-top"),
    ],
)
def test_prompt_precedence(
    tmp_path: Any,
    override: str | None,
    block_prompt: str | None,
    top_prompt: str | None,
    expected: str,
) -> None:
    """--prompt > cfg.image.prompt > cfg.prompt.

    Bug this catches: the precedence inverted so a config default silently
    overrides an explicit --prompt — the operator would be billed for an image
    of the wrong thing, with no error anywhere.
    """
    from kinoforge.core.image_run import generate_image

    data: dict[str, Any] = {"image": {"engine": "fake", "spec": {"model": "m"}}}
    if block_prompt is not None:
        data["image"]["prompt"] = block_prompt
    if top_prompt is not None:
        data["prompt"] = top_prompt

    engine = _SpyEngine()
    generate_image(
        Config.model_validate(data),
        store=_store(tmp_path),
        run_id="image-1",
        sink=None,
        prompt_override=override,
        image_engine=engine,
        image_profile_provider=_StubProvider(),
        http_get_bytes=lambda url, headers: PNG,
    )
    assert engine.backend_obj.submits[0].prompt == expected


def test_no_prompt_anywhere_is_refused_naming_both_fixes(tmp_path: Any) -> None:
    """A run with no resolvable prompt fails loudly at preflight.

    Bug this catches: submitting an empty prompt and paying the provider for
    whatever it decides that means.
    """
    from kinoforge.core.image_run import generate_image

    engine = _SpyEngine()
    with pytest.raises(ValidationError, match="--prompt"):
        generate_image(
            _cfg(),
            store=_store(tmp_path),
            run_id="image-1",
            sink=None,
            image_engine=engine,
            image_profile_provider=_StubProvider(),
            http_get_bytes=lambda url, headers: PNG,
        )
    assert engine.backend_obj.submits == []


def test_mode_gate_refuses_before_submit(tmp_path: Any) -> None:
    """A profile that does not support t2i stops the run BEFORE spending.

    Bug this catches: validating after submit, which still bills the provider.
    Asserting only pytest.raises would pass against that ordering, so the
    submits list is the real assertion here.
    """
    from kinoforge.core.image_run import generate_image

    engine = _SpyEngine()
    with pytest.raises(ValidationError, match="t2i"):
        generate_image(
            _cfg(prompt="a cat"),
            store=_store(tmp_path),
            run_id="image-1",
            sink=None,
            image_engine=engine,
            image_profile_provider=_StubProvider(profile=VIDEO_ONLY_PROFILE),
            http_get_bytes=lambda url, headers: PNG,
        )
    assert engine.backend_obj.submits == [], "submitted despite an unsupported mode"


def test_missing_image_block_is_refused(tmp_path: Any) -> None:
    """generate_image requires cfg.image.

    Bug this catches: an AttributeError on None deep in the function instead of
    a message telling the operator their config has no `image:` block.
    """
    from kinoforge.core.image_run import generate_image

    cfg = Config.model_validate(
        {
            "engine": {"kind": "fake", "precision": ""},
            "models": [{"kind": "base", "ref": "hf:a/b", "target": "checkpoints"}],
        }
    )
    with pytest.raises(ValidationError, match="image:"):
        generate_image(
            cfg,
            store=_store(tmp_path),
            run_id="image-1",
            sink=None,
            image_profile_provider=_StubProvider(),
        )


def test_empty_model_identity_warns_and_slugs_to_unknown(
    tmp_path: Any, caplog: Any
) -> None:
    """The successful-generations 17 trap: an empty identity became `_fal_unknown_`.

    Bug this catches: silently publishing `..._unknown_...` filenames, which is
    exactly what shipped for two keyframes in entry 17 and was invisible until
    a live run produced the files.
    """
    import logging

    from kinoforge.core.image_run import generate_image

    sink = _SpySink()
    with caplog.at_level(logging.WARNING):
        generate_image(
            _cfg(prompt="a cat"),
            store=_store(tmp_path),
            run_id="image-1",
            sink=sink,
            image_engine=_SpyEngine(identity=""),
            image_profile_provider=_StubProvider(),
            http_get_bytes=lambda url, headers: PNG,
        )
    # generate_image forwards the empty identity verbatim; LocalOutputSink is
    # what substitutes "unknown" (outputs/local.py), so the spy sees "".
    assert sink.published[0]["model"] == ""
    # NB getMessage(), not .message — LogRecord.message only exists once the
    # record has been formatted, so `r.message` raises AttributeError here.
    assert any("model_identity" in r.getMessage() for r in caplog.records), (
        "no warning emitted for an empty model_identity"
    )


def test_module_has_no_pipeline_machinery() -> None:
    """Structural guard: the design's central claim about this module.

    Bug this catches: reaching for PipelineState / deploy_session out of habit,
    which drags the dummy GenerationRequest(prompt="", mode="upscale") wart at
    orchestrator.py:2792 into a path that has nothing to chain.
    """
    from pathlib import Path

    import kinoforge.core.image_run as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("PipelineState", "deploy_session", "GenerationRequest"):
        assert forbidden not in source, (
            f"core/image_run.py references {forbidden}; the terminal image path "
            f"has nothing to chain and must not carry pipeline machinery"
        )
