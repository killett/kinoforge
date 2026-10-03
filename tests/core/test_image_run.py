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
    received_cancel_tokens: list[object] = field(default_factory=list)
    result_url: str = "https://fake-engine.example/out.png"

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
        # literal fixture. A `url` makes the http(s) branch fire, which is
        # what actually exercises the injected `http_get_bytes` seam the
        # tests pass in. The dead `meta={"_synthetic": True}` from the brief
        # is dropped — the synthetic branch is unreachable by construction
        # now that `url` is always set.
        #
        # Also records the `cancel_token` it was called with, so forwarding
        # from `generate_image` (the entire point of the preceding task) is
        # actually asserted rather than merely plumbed.
        self.received_cancel_tokens.append(cancel_token)
        return Artifact(filename="out.png", url=self.result_url)

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
    gets a file) — proven here by reading the stored name/path and bytes
    back, not merely by `artifact is not None` (which `put_bytes` always
    satisfies and so proves nothing; see the removed tautology below).

    Also proves, in one happy-path run: the published `provider` comes from
    the config's registry key (`cfg.image.engine`), not the engine's
    self-declared `.name` (they are deliberately made to differ here); the
    URL `artifact_bytes()` actually fetched; and that `namespace` /
    `cancel_token` are forwarded end to end.
    """
    from pathlib import Path

    from kinoforge.core.image_run import generate_image

    # Deliberately different from cfg.image.engine ("fake") so the provider
    # assertion below can tell "registry key" from "engine self-declaration"
    # apart — with the brief's un-corrected code (`provider=engine.name`)
    # this would assert "engine-declared-name" instead of "fake" and fail.
    engine = _SpyEngine(name="engine-declared-name")
    sink = _SpySink()
    fetched_urls: list[str] = []

    def _get_bytes(url: str, headers: dict[str, str]) -> bytes:
        fetched_urls.append(url)
        return PNG

    run_id = "image-20261003-120000"
    cancel_sentinel = object()
    artifact = generate_image(
        _cfg(prompt="a cat"),
        store=_store(tmp_path),
        run_id=run_id,
        sink=sink,
        namespace="batch-1",
        image_engine=engine,
        image_profile_provider=_StubProvider(),
        cancel_token=cancel_sentinel,  # type: ignore[arg-type]
        http_get_bytes=_get_bytes,
    )

    expected_path = (tmp_path / run_id / "image.png").resolve()
    assert Path(artifact.uri) == expected_path, (
        "stored artifact uri is not <run_id>/image.png — the fixed, "
        "non-prompt-derived name the `# kinoforge:public-name` pragma exists "
        "to enforce"
    )
    assert expected_path.read_bytes() == PNG, (
        "bytes on disk at the stored path are not the fetched PNG"
    )

    assert len(sink.published) == 1
    published = sink.published[0]
    assert published["kind"] == "image"
    assert published["extension"] == ".png"
    assert published["provider"] == "fake", (
        "provider must be the image.engine registry key, not engine.name"
    )
    assert published["model"] == "fake-model"
    assert published["bytes"] == PNG
    assert published["namespace"] == "batch-1"

    assert fetched_urls == [engine.backend_obj.result_url], (
        "http_get_bytes was not called with the artifact's own url"
    )
    assert engine.backend_obj.received_cancel_tokens == [cancel_sentinel], (
        "cancel_token was not forwarded to backend.result()"
    )


def test_sink_none_stores_only(tmp_path: Any) -> None:
    """--no-output-dir must still produce a stored artifact.

    Bug this catches: an unguarded self.sink.publish, which would make
    --no-output-dir crash with AttributeError on None (that part is caught
    for free — the exception would propagate through this test and fail
    it). The byte read-back is the half that `artifact is not None` alone
    cannot prove: `put_bytes` is contractually `-> Artifact`
    (`stores/base.py`) and always returns one, so that assertion is a
    tautology satisfied even by an implementation that never calls
    `put_bytes` at all.
    """
    from pathlib import Path

    from kinoforge.core.image_run import generate_image

    run_id = "image-1"
    artifact = generate_image(
        _cfg(prompt="a cat"),
        store=_store(tmp_path),
        run_id=run_id,
        sink=None,
        image_engine=_SpyEngine(),
        image_profile_provider=_StubProvider(),
        http_get_bytes=lambda url, headers: PNG,
    )
    expected_path = (tmp_path / run_id / "image.png").resolve()
    assert Path(artifact.uri) == expected_path
    assert expected_path.read_bytes() == PNG


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


def test_empty_model_identity_warns_and_passes_empty_through(
    tmp_path: Any, caplog: Any
) -> None:
    """The successful-generations 17 trap: an empty identity became `_fal_unknown_`.

    Bug this catches: silently publishing `..._unknown_...` filenames, which is
    exactly what shipped for two keyframes in entry 17 and was invisible until
    a live run produced the files. Renamed from
    `..._slugs_to_unknown`: `LocalOutputSink` (outputs/local.py), not this
    function, owns the `"unknown"` substitution — `generate_image` only
    forwards the empty string and logs a warning, which is exactly what this
    test asserts, so its name should not claim substitution coverage it does
    not exercise.
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

    Implemented as an AST import-node walk, not a raw-text grep over the
    whole file. A grep also matches prose in comments/docstrings — this
    file's own module docstring once had to avoid spelling out
    "PipelineState" purely to dodge its own guard, which is backwards: the
    guard should police the actual dependency, not the words used to explain
    its absence. A grep is also blind to indirection
    (`import kinoforge.core.orchestrator as x; x.deploy_session(...)` has no
    bare "deploy_session" substring issue but still drags in the forbidden
    module) and trivially dodged by string-building
    (`getattr(mod, "Pipeline" + "State")`). This version asserts the real
    contract: no import of `kinoforge.core.orchestrator` (home of
    `deploy_session`, and the module `PipelineState`/`GenerationRequest` are
    threaded through on the video side) and no import of the
    `PipelineState` / `GenerationRequest` names specifically from
    `kinoforge.core.interfaces` — which this file legitimately imports
    *other* names from (`Artifact`, `ImageJob`, ...), so a blanket
    module-level ban on `kinoforge.core.interfaces` would be wrong.
    """
    import ast
    from pathlib import Path

    import kinoforge.core.image_run as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=mod.__file__)

    forbidden_modules = ("kinoforge.core.orchestrator",)
    forbidden_names_from_interfaces = {"PipelineState", "GenerationRequest"}

    def _is_forbidden_module(name: str) -> bool:
        return any(
            name == forbidden or name.startswith(forbidden + ".")
            for forbidden in forbidden_modules
        )

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_module(alias.name):
                    violations.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _is_forbidden_module(module):
                violations.append(f"from {module} import ...")
            if module == "kinoforge.core.interfaces":
                for alias in node.names:
                    if alias.name in forbidden_names_from_interfaces:
                        violations.append(f"from {module} import {alias.name}")

    assert violations == [], (
        f"core/image_run.py imports pipeline machinery: {violations}; the "
        f"terminal image path has nothing to chain and must not carry it"
    )
