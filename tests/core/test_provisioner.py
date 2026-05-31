"""Tests for kinoforge.core.provisioner.

Acceptance criteria:
  AC1: With requires_local_weights=True and 3 entries: refs resolved, artifacts merged
       (sha256 + meta["target"]), merged list passed to injected downloader.
  AC2: With requires_local_weights=False: downloader NOT called; engine.provision IS called;
       sources ARE still queried (resolve called).
  AC3: post_provision_hook called once when provided; not called when None.
  AC4: engine.provision is the LAST call regardless of branch (ordering sentinel).
  AC5: UnknownAdapter bubbles out of provisioner when no source handles a ref.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kinoforge.core import registry
from kinoforge.core.errors import UnknownAdapter
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    CredentialProvider,
    GenerationBackend,
    GenerationJob,
    Instance,
    ModelProfile,
)
from kinoforge.core.provisioner import provision

# ---------------------------------------------------------------------------
# Shared test fakes
# ---------------------------------------------------------------------------


class _NullCreds(CredentialProvider):
    def get(self, key: str) -> str | None:  # noqa: D102
        return None


class _FakeSourceBase:
    """Base for test-only model sources; NOT a real ModelSource subclass.

    Tests register these via registry.register_source so the provisioner
    picks them up via source_for_ref().
    """

    def __init__(self, scheme: str) -> None:
        self.scheme = scheme
        self.resolve_calls: list[str] = []

    def handles(self, ref: str) -> bool:  # noqa: D102
        return ref.startswith(f"{self.scheme}:")

    def resolve(self, ref: str, creds: CredentialProvider) -> list[Artifact]:  # noqa: D102
        self.resolve_calls.append(ref)
        filename = ref.split(":")[-1]
        return [Artifact(filename=filename, url=ref)]


class _SpyEngine:
    """A spy GenerationEngine that records every method call into a shared list."""

    name: str = "spy"
    requires_compute: bool = False

    def __init__(self, call_log: list[str], *, requires_local_weights: bool) -> None:
        self.requires_local_weights = requires_local_weights
        self._log = call_log

    def provision(self, instance: Instance | None, cfg: Any) -> None:  # noqa: D102
        self._log.append("provision")

    # The remaining abstract methods are not exercised by provisioner tests.
    def backend(self, instance: Instance | None, cfg: Any) -> GenerationBackend:  # noqa: D102
        raise NotImplementedError

    def profile_for(self, key: CapabilityKey) -> ModelProfile:  # noqa: D102
        raise NotImplementedError

    def declared_flags(self, key: CapabilityKey) -> dict[str, bool]:  # noqa: D102
        return {}

    def validate_spec(self, job: GenerationJob) -> None:  # noqa: D102
        pass


class _ModelEntry:
    """Minimal model entry used as cfg.models items."""

    def __init__(self, ref: str, target: str, sha256: str | None = None) -> None:
        self.ref = ref
        self.target = target
        self.sha256 = sha256


class _FakeCfg:
    """Minimal provision config carrying a .models list."""

    def __init__(self, models: list[_ModelEntry]) -> None:
        self.models = models


def _spy_downloader(
    artifacts: list[Artifact], dest: Path, call_log: list[str]
) -> list[Artifact]:
    """Record the call, tag each artifact uri, and return them."""
    call_log.append("download")
    return [
        Artifact(**{**a.__dict__, "uri": f"{dest}/{a.filename}"}) for a in artifacts
    ]


def _make_instance() -> Instance:
    return Instance(id="i-test", provider="fake", status="ready", created_at=0.0)


# ---------------------------------------------------------------------------
# AC1: requires_local_weights=True — refs resolved, artifacts merged, downloader called
# ---------------------------------------------------------------------------


def test_ac1_downloads_when_weights_required(tmp_path: Path) -> None:
    """Downloader is called once with merged artifacts from all 3 entries."""
    scheme = "ac1fake"
    source = _FakeSourceBase(scheme)
    registry.register_source(source)  # type: ignore[arg-type]

    entries = [
        _ModelEntry(ref=f"{scheme}:model-a", target="unet", sha256="aaa111"),
        _ModelEntry(ref=f"{scheme}:model-b", target="vae", sha256=None),
        _ModelEntry(ref=f"{scheme}:model-c", target="lora0", sha256="ccc333"),
    ]
    cfg = _FakeCfg(entries)

    call_log: list[str] = []
    engine = _SpyEngine(call_log, requires_local_weights=True)
    instance = _make_instance()
    creds = _NullCreds()

    downloaded_artifacts: list[list[Artifact]] = []

    def spy_dl(artifacts: list[Artifact], dest: Path) -> list[Artifact]:
        call_log.append("download")
        downloaded_artifacts.append(list(artifacts))
        return [
            Artifact(**{**a.__dict__, "uri": f"{dest}/{a.filename}"}) for a in artifacts
        ]

    provision(
        engine,  # type: ignore[arg-type]
        cfg,  # type: ignore[arg-type]
        instance,
        creds=creds,
        download_dir=tmp_path,
        downloader=spy_dl,
    )

    # Downloader called exactly once
    assert call_log.count("download") == 1

    # All 3 artifacts were passed (one per entry in this fixture)
    assert len(downloaded_artifacts[0]) == 3

    # Artifact merge: sha256 and meta["target"] set correctly
    by_filename = {a.filename: a for a in downloaded_artifacts[0]}

    a_art = by_filename["model-a"]
    assert a_art.sha256 == "aaa111"
    assert a_art.meta["target"] == "unet"

    b_art = by_filename["model-b"]
    assert b_art.sha256 is None  # entry has no sha256 — stays None
    assert b_art.meta["target"] == "vae"

    c_art = by_filename["model-c"]
    assert c_art.sha256 == "ccc333"
    assert c_art.meta["target"] == "lora0"

    # source.resolve was called for each entry
    assert source.resolve_calls == [
        f"{scheme}:model-a",
        f"{scheme}:model-b",
        f"{scheme}:model-c",
    ]


# ---------------------------------------------------------------------------
# AC2: requires_local_weights=False — no download, engine.provision still called,
#      sources still queried
# ---------------------------------------------------------------------------


def test_ac2_no_download_but_resolve_and_provision_called(tmp_path: Path) -> None:
    """Downloader NOT called; resolve IS called; engine.provision IS called."""
    scheme = "ac2fake"
    source = _FakeSourceBase(scheme)
    registry.register_source(source)  # type: ignore[arg-type]

    entries = [
        _ModelEntry(ref=f"{scheme}:model-x", target="base"),
        _ModelEntry(ref=f"{scheme}:model-y", target="lora0"),
    ]
    cfg = _FakeCfg(entries)

    call_log: list[str] = []
    engine = _SpyEngine(call_log, requires_local_weights=False)
    instance = _make_instance()
    creds = _NullCreds()

    dl_call_count = 0

    def spy_dl(artifacts: list[Artifact], dest: Path) -> list[Artifact]:
        nonlocal dl_call_count
        dl_call_count += 1
        return artifacts

    provision(
        engine,  # type: ignore[arg-type]
        cfg,  # type: ignore[arg-type]
        instance,
        creds=creds,
        download_dir=tmp_path,
        downloader=spy_dl,
    )

    # Downloader NOT called
    assert dl_call_count == 0

    # engine.provision WAS called
    assert "provision" in call_log

    # sources were still queried for resolve
    assert source.resolve_calls == [f"{scheme}:model-x", f"{scheme}:model-y"]


# ---------------------------------------------------------------------------
# AC3: post_provision_hook — called once when provided; not called when None
# ---------------------------------------------------------------------------


def test_ac3_hook_called_when_provided(tmp_path: Path) -> None:
    """post_provision_hook is invoked exactly once."""
    scheme = "ac3fake"
    source = _FakeSourceBase(scheme)
    registry.register_source(source)  # type: ignore[arg-type]

    cfg = _FakeCfg([_ModelEntry(ref=f"{scheme}:m", target="base")])
    call_log: list[str] = []
    engine = _SpyEngine(call_log, requires_local_weights=False)
    instance = _make_instance()

    hook_calls: list[Instance | None] = []

    def hook(inst: Instance | None) -> None:
        hook_calls.append(inst)

    provision(
        engine,  # type: ignore[arg-type]
        cfg,  # type: ignore[arg-type]
        instance,
        creds=_NullCreds(),
        download_dir=tmp_path,
        post_provision_hook=hook,
    )

    assert len(hook_calls) == 1
    assert hook_calls[0] is instance


def test_ac3_hook_not_called_when_none(tmp_path: Path) -> None:
    """No hook call when post_provision_hook=None (default)."""
    scheme = "ac3bfake"
    source = _FakeSourceBase(scheme)
    registry.register_source(source)  # type: ignore[arg-type]

    cfg = _FakeCfg([_ModelEntry(ref=f"{scheme}:m", target="base")])
    call_log: list[str] = []
    engine = _SpyEngine(call_log, requires_local_weights=False)

    # No hook passed (defaults to None)
    provision(
        engine,  # type: ignore[arg-type]
        cfg,  # type: ignore[arg-type]
        _make_instance(),
        creds=_NullCreds(),
        download_dir=tmp_path,
    )

    # We assert absence: if provision() ran without exception, hook was not invoked.
    # "provision" must still be in call_log to confirm normal execution.
    assert "provision" in call_log


# ---------------------------------------------------------------------------
# AC4: engine.provision is the LAST call regardless of branch
# ---------------------------------------------------------------------------


def test_ac4_provision_is_last_with_downloads(tmp_path: Path) -> None:
    """With downloads: order is resolve → download → hook → provision (LAST)."""
    scheme = "ac4afake"
    source = _FakeSourceBase(scheme)
    registry.register_source(source)  # type: ignore[arg-type]

    cfg = _FakeCfg([_ModelEntry(ref=f"{scheme}:m", target="base", sha256="abc")])
    call_log: list[str] = []
    engine = _SpyEngine(call_log, requires_local_weights=True)

    def spy_dl(artifacts: list[Artifact], dest: Path) -> list[Artifact]:
        call_log.append("download")
        return artifacts

    def hook(inst: Instance | None) -> None:
        call_log.append("hook")

    provision(
        engine,  # type: ignore[arg-type]
        cfg,  # type: ignore[arg-type]
        _make_instance(),
        creds=_NullCreds(),
        download_dir=tmp_path,
        post_provision_hook=hook,
        downloader=spy_dl,
    )

    # provision must come after download and hook
    assert call_log[-1] == "provision"
    assert call_log.index("download") < call_log.index("provision")
    assert call_log.index("hook") < call_log.index("provision")


def test_ac4_provision_is_last_without_downloads(tmp_path: Path) -> None:
    """Without downloads: order is resolve → hook → provision (LAST)."""
    scheme = "ac4bfake"
    source = _FakeSourceBase(scheme)
    registry.register_source(source)  # type: ignore[arg-type]

    cfg = _FakeCfg([_ModelEntry(ref=f"{scheme}:m", target="base")])
    call_log: list[str] = []
    engine = _SpyEngine(call_log, requires_local_weights=False)

    def hook(inst: Instance | None) -> None:
        call_log.append("hook")

    provision(
        engine,  # type: ignore[arg-type]
        cfg,  # type: ignore[arg-type]
        _make_instance(),
        creds=_NullCreds(),
        download_dir=tmp_path,
        post_provision_hook=hook,
    )

    assert call_log[-1] == "provision"
    assert call_log.index("hook") < call_log.index("provision")


# ---------------------------------------------------------------------------
# AC5: UnknownAdapter bubbles up when no source handles a ref
# ---------------------------------------------------------------------------


def test_pydantic_cfg_is_dumped_to_dict_before_engine_provision(
    tmp_path: Path,
) -> None:
    """A pydantic-style cfg (exposes ``model_dump``) must be dumped to a plain
    dict before being forwarded to ``engine.provision``.

    Bug catch: engines call ``cfg.get("engine", {})`` and other ``dict`` methods
    on the cfg they receive (per the ``GenerationEngine`` ABC contract).  Before
    the fix, the provisioner forwarded the raw pydantic ``Config`` object,
    causing ``AttributeError: 'Config' object has no attribute 'get'`` at the
    first hosted/diffusers/comfyui ``provision`` call site in production.
    """
    scheme = "pydcfgfake"
    source = _FakeSourceBase(scheme)
    registry.register_source(source)  # type: ignore[arg-type]

    received_cfgs: list[Any] = []

    class _RecordingEngine(_SpyEngine):
        def provision(self, instance: Instance | None, cfg: Any) -> None:  # noqa: D102
            received_cfgs.append(cfg)
            super().provision(instance, cfg)

    class _PydanticLikeCfg:
        """Stand-in for pydantic ``Config`` exposing both ``.models`` and
        ``.model_dump()`` — mirrors the real Config object's surface area
        without depending on pydantic in the test."""

        def __init__(self, models: list[_ModelEntry], dump: dict[str, Any]) -> None:
            self.models = models
            self._dump = dump

        def model_dump(self) -> dict[str, Any]:
            return self._dump

    dump_payload = {"engine": {"kind": "spy", "hosted": {"api_key_env": "FOO"}}}
    cfg = _PydanticLikeCfg(
        models=[_ModelEntry(ref=f"{scheme}:m", target="base")],
        dump=dump_payload,
    )
    engine = _RecordingEngine([], requires_local_weights=False)

    provision(
        engine,  # type: ignore[arg-type]
        cfg,  # type: ignore[arg-type]
        _make_instance(),
        creds=_NullCreds(),
        download_dir=tmp_path,
    )

    assert len(received_cfgs) == 1, "engine.provision should have been called once"
    forwarded = received_cfgs[0]
    # Must be a plain dict (so cfg.get(...) works) and must equal the dump payload.
    assert isinstance(forwarded, dict), (
        f"expected dict, got {type(forwarded).__name__}: pydantic cfg leaked through"
    )
    assert forwarded == dump_payload
    # Sanity: a plain dict cfg (no model_dump) should still pass through unchanged.
    plain = _FakeCfg([_ModelEntry(ref=f"{scheme}:n", target="base")])
    received_cfgs.clear()
    provision(
        engine,  # type: ignore[arg-type]
        plain,  # type: ignore[arg-type]
        _make_instance(),
        creds=_NullCreds(),
        download_dir=tmp_path,
    )
    # _FakeCfg has no model_dump → forwarded as-is.
    assert received_cfgs[0] is plain


def test_ac5_unknown_ref_raises_unknown_adapter(tmp_path: Path) -> None:
    """Provisioner does NOT catch UnknownAdapter — it propagates to the caller."""
    cfg = _FakeCfg([_ModelEntry(ref="totally-unknown-scheme:model", target="base")])
    call_log: list[str] = []
    engine = _SpyEngine(call_log, requires_local_weights=False)

    with pytest.raises(UnknownAdapter):
        provision(
            engine,  # type: ignore[arg-type]
            cfg,  # type: ignore[arg-type]
            _make_instance(),
            creds=_NullCreds(),
            download_dir=tmp_path,
        )
