"""Unit tests for ``core.lora_apply`` — the controller-side stack applier.

The module under test is the single place that turns a resolved
``loras:`` stack into one ``POST /lora/set_stack`` against a ready pod.
Everything here pins one of two things:

* the wire contract — one call, the resolved stack, controller-resolved
  download specs (so a CivitAI token never leaves the controller);
* the privacy contract — a LoRA ref is SENSITIVE under vault mode, so no
  log line this module emits may carry one.

Every test names the concrete regression it catches.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

# Import the sources so they self-register with ``core.registry``.
import kinoforge.sources.civitai  # noqa: F401
import kinoforge.sources.http  # noqa: F401
import kinoforge.sources.huggingface  # noqa: F401
from kinoforge.core import registry
from kinoforge.core.config import Config, load_config
from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.errors import LoraFormatUnsupportedError, ValidationError
from kinoforge.core.interfaces import Artifact, CredentialProvider, ModelSource
from kinoforge.core.lora import LoraEntry
from kinoforge.core.lora_apply import ensure_lora_stack, resolve_download_specs
from kinoforge.sources.civitai import CivitAISource

# A ref shaped like the sensitive thing the privacy rule protects. Chosen
# so a substring search cannot pass by accident.
_SENSITIVE_REF = "civitai:9876543210@1234567890"

_CFG_HEAD = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake-base.safetensors"
    kind: base
    target: diffusion_models
"""

_CFG_TAIL = """\
compute:
  provider: local
  image: fake:latest
  warm_reuse_auto_attach: false
  lifecycle:
    budget: 1.0
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _NullCreds(CredentialProvider):
    """Credential provider that knows nothing — keeps resolves hermetic."""

    def get(self, key: str) -> str | None:
        """Return ``None`` for every key.

        Args:
            key: Ignored.

        Returns:
            Always ``None``.
        """
        del key
        return None


class _SpyLoraBackend:
    """Backend stub recording every ``set_lora_stack`` call.

    Attributes:
        calls: One tuple ``(pod_id, active_stack, download_specs)`` per
            invocation.
    """

    def __init__(self, *, raises: Exception | None = None) -> None:
        """Record calls; optionally raise ``raises`` after recording.

        Args:
            raises: Exception to raise once the call is recorded, so a
                test can prove the failure is not swallowed.
        """
        self.calls: list[tuple[str, list[LoraEntry], dict[str, Any]]] = []
        self._raises = raises

    def set_lora_stack(
        self,
        *,
        pod_id: str,
        active_stack: list[LoraEntry],
        download_specs: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Record the call and return an empty-inventory success body.

        Args:
            pod_id: Pod the stack is aimed at.
            active_stack: The resolved, ordered stack.
            download_specs: Controller-resolved per-ref download specs.

        Returns:
            A minimal success body.

        Raises:
            Exception: The instance's configured ``raises``, if any.
        """
        self.calls.append((pod_id, list(active_stack), dict(download_specs)))
        if self._raises is not None:
            raise self._raises
        return {"inventory": [], "free_bytes": 0}


class _HostedBackend:
    """Backend with no LoRA surface at all — the hosted-engine shape."""

    def submit(self) -> str:
        """Return a fixed job id (present only so the class is not empty)."""
        return "job-1"


class _StubSource(ModelSource):
    """Model source returning canned artifacts for one scheme."""

    def __init__(self, *, scheme: str, artifacts: list[Artifact]) -> None:
        """Bind the scheme this source claims and what it resolves to.

        Args:
            scheme: Registry scheme key + the ref prefix claimed.
            artifacts: Returned verbatim by every ``resolve``.
        """
        self.scheme = scheme
        self._artifacts = artifacts

    def handles(self, ref: str) -> bool:
        """Return True when ``ref`` starts with ``<scheme>:``.

        Args:
            ref: The ref being routed.

        Returns:
            Whether this source claims it.
        """
        return ref.startswith(f"{self.scheme}:")

    def resolve(self, ref: str, creds: CredentialProvider) -> list[Artifact]:
        """Return the canned artifact list.

        Args:
            ref: Ignored.
            creds: Ignored.

        Returns:
            The artifacts supplied at construction.
        """
        del ref, creds
        return list(self._artifacts)


@pytest.fixture
def restore_sources() -> Any:  # noqa: ANN401 — pytest fixture generator
    """Re-register the shipped sources after a test swaps one out.

    ``registry.register_source`` replaces by ``scheme``, so a test that
    installs a stub for ``civitai`` must put the real one back or it
    leaks into every later test in the session.

    Yields:
        ``None`` — the fixture exists for its teardown.
    """
    yield None
    registry.register_source(CivitAISource())


def _cfg(lora_refs: list[str]) -> Config:
    """Build a fake-engine config carrying ``lora_refs`` in ``loras:``.

    Args:
        lora_refs: Refs to write into the ``loras:`` block; an empty list
            omits the block entirely.

    Returns:
        The loaded :class:`Config`.
    """
    block = ""
    if lora_refs:
        lines = "\n".join(f'  - ref: "{r}"\n    strength: 0.8' for r in lora_refs)
        block = f"loras:\n{lines}\n"
    return load_config(_CFG_HEAD + block + _CFG_TAIL)


# ---------------------------------------------------------------------------
# resolve_download_specs
# ---------------------------------------------------------------------------


def test_resolve_download_specs_shapes_hf_and_civitai_alike(
    restore_sources: None,
) -> None:
    """Both vendors must produce the pod's four-key spec shape.

    Bug caught: a vendor-specific key name (``size`` instead of
    ``size_hint``, or a missing ``headers``) makes the pod reject the
    body with a 422 for that vendor only — invisible until a live run
    against that exact source.
    """
    del restore_sources
    registry.register_source(
        _StubSource(
            scheme="civitai",
            artifacts=[
                Artifact(
                    url="https://civitai.example/download/1",
                    filename="style.safetensors",
                    size=4096,
                    headers={"Authorization": "Bearer placeholder"},
                )
            ],
        )
    )

    specs = resolve_download_specs(
        ["hf:org/repo:adapter.safetensors", "civitai:11@22"], _NullCreds()
    )

    assert set(specs) == {"hf:org/repo:adapter.safetensors", "civitai:11@22"}
    for spec in specs.values():
        assert set(spec) == {"url", "headers", "filename", "size_hint"}
    hf_spec = specs["hf:org/repo:adapter.safetensors"]
    assert hf_spec["url"] == (
        "https://huggingface.co/org/repo/resolve/main/adapter.safetensors"
    )
    assert hf_spec["filename"] == "adapter.safetensors"
    civitai_spec = specs["civitai:11@22"]
    assert civitai_spec["url"] == "https://civitai.example/download/1"
    assert civitai_spec["filename"] == "style.safetensors"
    assert civitai_spec["size_hint"] == 4096
    assert civitai_spec["headers"] == {"Authorization": "Bearer placeholder"}


def test_resolve_download_specs_picks_the_safetensors_artifact(
    restore_sources: None,
) -> None:
    """A multi-file version must resolve to its ``.safetensors`` member.

    Bug caught: picking ``artifacts[0]`` blindly ships the pod a preview
    image or a ``.ckpt`` companion, which then fails to load as a LoRA
    with a format error that looks like a bad ref.
    """
    del restore_sources
    registry.register_source(
        _StubSource(
            scheme="civitai",
            artifacts=[
                Artifact(url="https://x/preview.png", filename="preview.png"),
                Artifact(url="https://x/w.safetensors", filename="w.safetensors"),
            ],
        )
    )

    specs = resolve_download_specs(["civitai:11@22"], _NullCreds())

    assert specs["civitai:11@22"]["filename"] == "w.safetensors"


def test_resolve_download_specs_falls_back_when_no_safetensors(
    restore_sources: None,
) -> None:
    """A source with no ``.safetensors`` member still yields a spec.

    Bug caught: a ``next(...)`` with no default raises ``StopIteration``
    on ``.ckpt``-only packs, aborting the run with an opaque traceback.
    """
    del restore_sources
    registry.register_source(
        _StubSource(
            scheme="civitai",
            artifacts=[Artifact(url="https://x/w.ckpt", filename="w.ckpt")],
        )
    )

    specs = resolve_download_specs(["civitai:11@22"], _NullCreds())

    assert specs["civitai:11@22"]["filename"] == "w.ckpt"


def test_resolve_download_specs_refuses_an_empty_resolve_without_naming_the_ref(
    restore_sources: None,
) -> None:
    """A ref resolving to nothing must raise, and not name the ref.

    Bug caught (correctness): an empty artifact list silently omits that
    ref from ``download_specs``, so the pod is asked to activate a LoRA
    it was never told how to fetch. Bug caught (privacy): the refusal
    message echoes a vault ref into the operator's terminal.
    """
    del restore_sources
    registry.register_source(_StubSource(scheme="civitai", artifacts=[]))

    with pytest.raises(ValidationError) as exc:
        resolve_download_specs([_SENSITIVE_REF], _NullCreds())

    assert _SENSITIVE_REF not in str(exc.value)
    assert "9876543210" not in str(exc.value)


def test_resolve_download_specs_resolves_a_repeated_ref_once(
    restore_sources: None,
) -> None:
    """A ref appearing twice must cost one resolve, not two.

    Bug caught: a per-entry resolve doubles the CivitAI API calls for a
    stack that names the same adapter at two strengths, which is the
    fastest way to hit that API's rate limit.
    """
    del restore_sources

    class _CountingSource(_StubSource):
        def __init__(self) -> None:
            super().__init__(
                scheme="civitai",
                artifacts=[Artifact(url="https://x/w.safetensors", filename="w.st")],
            )
            self.resolve_calls = 0

        def resolve(self, ref: str, creds: CredentialProvider) -> list[Artifact]:
            self.resolve_calls += 1
            return super().resolve(ref, creds)

    source = _CountingSource()
    registry.register_source(source)

    specs = resolve_download_specs(["civitai:11@22", "civitai:11@22"], _NullCreds())

    assert source.resolve_calls == 1
    assert set(specs) == {"civitai:11@22"}


# ---------------------------------------------------------------------------
# ensure_lora_stack
# ---------------------------------------------------------------------------


def test_empty_stack_never_touches_the_backend() -> None:
    """A config with no ``loras:`` must issue zero LoRA HTTP calls.

    Bug caught: an unconditional ``set_lora_stack`` POST on every run.
    Against a server build with no ``/lora/set_stack`` route that is a
    404 which breaks every run working today.
    """
    backend = _SpyLoraBackend()

    ensure_lora_stack(backend=backend, cfg=_cfg([]), pod_id="p1", creds=_NullCreds())

    assert backend.calls == []


def test_non_empty_stack_posts_exactly_one_set_lora_stack() -> None:
    """The resolved stack reaches the pod in one call, with its specs.

    Bug caught: a per-entry POST (N calls, each replacing the last, so
    only the final LoRA survives), or a call that ships the stack with
    no ``download_specs`` so the pod cannot fetch anything.
    """
    backend = _SpyLoraBackend()
    cfg = _cfg(["hf:org/repo:a.safetensors", "hf:org/repo:b.safetensors"])

    ensure_lora_stack(backend=backend, cfg=cfg, pod_id="p1", creds=_NullCreds())

    assert len(backend.calls) == 1
    pod_id, active_stack, specs = backend.calls[0]
    assert pod_id == "p1"
    assert [e.ref for e in active_stack] == [
        "hf:org/repo:a.safetensors",
        "hf:org/repo:b.safetensors",
    ]
    assert [e.strength for e in active_stack] == [0.8, 0.8]
    assert set(specs) == {"hf:org/repo:a.safetensors", "hf:org/repo:b.safetensors"}
    assert specs["hf:org/repo:a.safetensors"]["filename"] == "a.safetensors"


def test_backend_without_set_lora_stack_is_a_noop() -> None:
    """A hosted backend has no LoRA surface; applying must not crash.

    Bug caught: an unguarded ``backend.set_lora_stack(...)`` turns every
    hosted-engine run whose config carries ``loras:`` into an
    ``AttributeError`` before the first frame is generated.
    """
    backend = _HostedBackend()
    cfg = _cfg(["hf:org/repo:a.safetensors"])

    ensure_lora_stack(backend=backend, cfg=cfg, pod_id=None, creds=_NullCreds())


def test_pod_id_none_does_not_post_to_a_lora_capable_backend() -> None:
    """With no pod there is nothing to address; do not fabricate one.

    Bug caught: passing ``pod_id=None`` through to the wire, where it
    lands in an exception's "kinoforge destroy --id None" recovery hint.
    """
    backend = _SpyLoraBackend()
    cfg = _cfg(["hf:org/repo:a.safetensors"])

    ensure_lora_stack(backend=backend, cfg=cfg, pod_id=None, creds=_NullCreds())

    assert backend.calls == []


def test_format_failure_propagates_untouched() -> None:
    """A pod refusal must escape, not degrade into a warning.

    Bug caught: a ``try/except Exception: log.warning(...)`` around the
    apply. That is precisely the silent defect this module exists to
    remove — the run would carry on and produce a LoRA-less video.
    """
    boom = LoraFormatUnsupportedError(
        pod_id="p1", ref="hf:org/repo:a.safetensors", hint="use the diffusers format"
    )
    backend = _SpyLoraBackend(raises=boom)
    cfg = _cfg(["hf:org/repo:a.safetensors"])

    with pytest.raises(LoraFormatUnsupportedError) as exc:
        ensure_lora_stack(backend=backend, cfg=cfg, pod_id="p1", creds=_NullCreds())

    assert exc.value is boom


def test_cli_override_beats_the_config_stack() -> None:
    """The applier must honour CLI > vault > cfg, not read cfg directly.

    Bug caught: reading ``cfg.loras`` instead of calling
    ``resolve_active_lora_stack``, so ``--loras`` is accepted on the
    command line and then quietly ignored on the pod.
    """
    backend = _SpyLoraBackend()
    cfg = _cfg(["hf:org/repo:from-cfg.safetensors"])
    override = [LoraEntry(ref="hf:org/repo:from-cli.safetensors", strength=0.25)]

    with EphemeralSession(enabled=False) as session:
        session.cli_loras = list(override)
        ensure_lora_stack(backend=backend, cfg=cfg, pod_id="p1", creds=_NullCreds())

    assert len(backend.calls) == 1
    _pod_id, active_stack, specs = backend.calls[0]
    assert [e.ref for e in active_stack] == ["hf:org/repo:from-cli.safetensors"]
    assert set(specs) == {"hf:org/repo:from-cli.safetensors"}


def test_creds_default_to_the_environment_when_none() -> None:
    """``creds=None`` must still resolve, not crash on ``None.get``.

    Bug caught: ``deploy_session`` may be called without a credential
    provider (its parameter is optional), so a non-defaulted ``creds``
    would raise ``AttributeError: 'NoneType' object has no attribute
    'get'`` for every LoRA run that did not pass one.
    """
    backend = _SpyLoraBackend()
    cfg = _cfg(["hf:org/repo:a.safetensors"])

    ensure_lora_stack(backend=backend, cfg=cfg, pod_id="p1", creds=None)

    assert len(backend.calls) == 1


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------


def test_no_log_line_carries_a_ref_on_the_success_path(
    caplog: pytest.LogCaptureFixture,
    restore_sources: None,
) -> None:
    """Refs are SENSITIVE under vault mode — log counts, never refs.

    Bug caught: a debug line such as ``"applying %s", stack`` that dumps
    the whole stack. ``LoraEntry.__repr__`` contains the ref, so the
    vault ref lands in the operator's terminal and in any log file.
    """
    del restore_sources
    registry.register_source(
        _StubSource(
            scheme="civitai",
            artifacts=[Artifact(url="https://x/w.safetensors", filename="w.st")],
        )
    )
    backend = _SpyLoraBackend()
    override = [LoraEntry(ref=_SENSITIVE_REF, strength=1.0)]

    with caplog.at_level(logging.DEBUG, logger="kinoforge.core.lora_apply"):
        with EphemeralSession(enabled=False) as session:
            session.cli_loras = list(override)
            ensure_lora_stack(
                backend=backend,
                cfg=_cfg([]),
                pod_id="p1",
                creds=_NullCreds(),
            )

    assert len(backend.calls) == 1, "the apply must actually have happened"
    assert caplog.records, "expected at least one log record to inspect"
    assert _SENSITIVE_REF not in caplog.text
    assert "9876543210" not in caplog.text
    assert "1234567890" not in caplog.text


def test_no_log_line_carries_a_ref_when_the_pod_refuses(
    caplog: pytest.LogCaptureFixture,
    restore_sources: None,
) -> None:
    """The failure path must not leak the ref it is failing on either.

    Bug caught: an ``except ... as exc: log.error("apply failed: %s",
    exc)`` re-log. ``LoraFormatUnsupportedError.__str__`` names the ref
    by design (it is the operator's recovery hint at the CLI boundary),
    so re-logging it here writes a vault ref to the log file.
    """
    del restore_sources
    registry.register_source(
        _StubSource(
            scheme="civitai",
            artifacts=[Artifact(url="https://x/w.safetensors", filename="w.st")],
        )
    )
    backend = _SpyLoraBackend(
        raises=LoraFormatUnsupportedError(
            pod_id="p1", ref=_SENSITIVE_REF, hint="wrong lineage"
        )
    )
    override = [LoraEntry(ref=_SENSITIVE_REF, strength=1.0)]

    with caplog.at_level(logging.DEBUG, logger="kinoforge.core.lora_apply"):
        with EphemeralSession(enabled=False) as session:
            session.cli_loras = list(override)
            with pytest.raises(LoraFormatUnsupportedError):
                ensure_lora_stack(
                    backend=backend,
                    cfg=_cfg([]),
                    pod_id="p1",
                    creds=_NullCreds(),
                )

    assert _SENSITIVE_REF not in caplog.text
    assert "9876543210" not in caplog.text


def test_hosted_noop_log_line_carries_no_ref(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The hosted-engine discard must be visible without naming refs.

    Bug caught: making the "this backend cannot load LoRAs" notice
    useful by listing what was dropped — which is exactly the leak.
    """
    override = [LoraEntry(ref=_SENSITIVE_REF, strength=1.0)]

    with caplog.at_level(logging.DEBUG, logger="kinoforge.core.lora_apply"):
        with EphemeralSession(enabled=False) as session:
            session.cli_loras = list(override)
            ensure_lora_stack(
                backend=_HostedBackend(),
                cfg=_cfg([]),
                pod_id=None,
                creds=_NullCreds(),
            )

    assert _SENSITIVE_REF not in caplog.text
    assert "9876543210" not in caplog.text
