"""Behavior: a LoRA stack that cannot resolve fails BEFORE the pod is booked.

U57. Three LoRA-resolution failure classes fired only from inside
``ensure_lora_stack``, which ``orchestrator.deploy_session`` calls *after* the
pod reports ready — a call site whose own comment says "The LoRA stack is
applied HERE — after the pod reports ready and before any job is submitted".
They are loud (D7: every one raises and fails the run rather than silently
producing a LoRA-less video), but by the time any of them can fire
``create_instance`` has already run and an H200 is already billing:

* ``LoraStackConflict`` — cfg.loras and vault.loras both non-empty with
  diverging refs (``core/lora.py``).
* ``ValidationError`` — a ref that resolves to zero downloadable artifacts
  (``core/lora_apply.resolve_download_specs``).
* ``UnknownAdapter`` — no registered source handles the ref
  (``core/registry.source_for_ref``).

None of the three depends on anything the pod reports back:
``resolve_active_lora_stack`` reads only cfg/vault/CLI, and
``resolve_download_specs`` reads only the ref plus credentials. So all three are
answerable before any spend.

**Why the existing eager call does not already cover this.** ``_cmd_generate``
calls ``resolve_active_lora_stack`` before preflight — but only inside
``if _raw_loras is not None``, i.e. only when ``--loras`` was passed. In that
case ``cli_loras is not None``, so the resolver returns early and
``LoraStackConflict`` is unreachable by construction: the conflict fires only
when ``cli_loras is None``, which is exactly the branch that eager call skips.

Vault and CLI stack are read off the ambient ``EphemeralSession``, the same way
``core/lora_apply.ensure_lora_stack`` and ``core/warm_reuse/integration`` read
them, so no check-protocol change is needed to see them.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from kinoforge.core.config import Config
from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.validation.protocol import CheckCategory, Severity

_VAULT_REF = "civitai:9999@8888"


def _cfg(loras: list[dict[str, Any]]) -> Config:
    """Build the minimum diffusers cfg that validates, carrying *loras*."""
    return Config.model_validate(
        {
            "mode": "t2v",
            "engine": {
                "kind": "diffusers",
                "precision": "fp8",
                "diffusers": {
                    "server_cmd": [
                        "python",
                        "-m",
                        "kinoforge.engines.diffusers.servers.wan_t2v_server",
                    ],
                    "base_url": "http://localhost:8000",
                },
            },
            "models": [
                {"ref": "hf:org/repo", "kind": "base", "target": "diffusion_models"}
            ],
            "loras": loras,
        }
    )


class _Vault:
    """Minimal stand-in for a loaded Vault — only ``.loras`` is read."""

    def __init__(self, refs: list[str]) -> None:
        self.loras = [
            Config.model_validate(
                {
                    "mode": "t2v",
                    "engine": {"kind": "diffusers", "precision": "fp8"},
                    "models": [
                        {
                            "ref": "hf:org/repo",
                            "kind": "base",
                            "target": "diffusion_models",
                        }
                    ],
                    "loras": [{"ref": r}],
                }
            ).loras[0]
            for r in refs
        ]


class _Session:
    """Stand-in EphemeralSession exposing only what the checks read."""

    def __init__(self, vault: Any = None, cli_loras: Any = None) -> None:
        self.vault = vault
        self.cli_loras = cli_loras


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[_Session | None]]:
    """Install a settable ambient session for the checks to read."""
    holder: list[_Session | None] = [None]
    monkeypatch.setattr(
        EphemeralSession, "current", staticmethod(lambda: holder[0]), raising=False
    )
    yield holder


# ---------------------------------------------------------------------------
# LoraStackConflictCheck
# ---------------------------------------------------------------------------


def test_a_diverging_cfg_and_vault_stack_fails_before_any_spend(
    session: list[_Session | None],
) -> None:
    """The defect. A conflict must not cost an H200 boot to discover.

    Bug caught: leaving ``LoraStackConflict`` reachable only from
    ``ensure_lora_stack``, which runs after ``create_instance``. Nothing in
    this decision needs the pod — it reads cfg and vault and nothing else.
    """
    from kinoforge.validation.checks.loras import LoraStackConflictCheck

    session[0] = _Session(vault=_Vault([_VAULT_REF]))
    cfg = _cfg([{"ref": "civitai:1111@2222"}])
    check = LoraStackConflictCheck()

    assert check.applies_to(cfg) is True
    result = check.run(cfg)

    assert result.passed is False
    assert result.severity is Severity.ERROR


def test_the_conflict_message_never_names_a_vault_ref(
    session: list[_Session | None],
) -> None:
    """A vault ref is a secret; the operator-facing message must not carry it.

    Bug caught: formatting the resolver's exception straight into
    ``CheckResult.message``. ``LoraStackConflict``'s own text lists both ref
    sets — appropriate for a traceback under an active RedactionRegistry,
    but this message is printed to stderr by ``_cmd_generate`` and would put
    a vault ref in front of anyone reading the terminal. ``resolve_download_
    specs`` already takes this care ("names the stack position instead,
    because the ref may be a vault secret"); a new surface must not undo it.
    """
    from kinoforge.validation.checks.loras import LoraStackConflictCheck

    session[0] = _Session(vault=_Vault([_VAULT_REF]))
    cfg = _cfg([{"ref": "civitai:1111@2222"}])

    result = LoraStackConflictCheck().run(cfg)

    assert _VAULT_REF not in result.message
    assert "civitai:1111@2222" not in result.message


def test_a_vault_that_mirrors_the_cfg_stack_is_not_a_conflict(
    session: list[_Session | None],
) -> None:
    """Negative control — agreeing stacks are legal and must stay legal.

    Bug caught: a check that rejects any cfg+vault combination. The resolver
    explicitly permits a vault that mirrors cfg (it compares ref SETS, not
    presence), and failing it would break every run of that shape at
    preflight — a check that costs more than the defect.
    """
    from kinoforge.validation.checks.loras import LoraStackConflictCheck

    shared = "civitai:1111@2222"
    session[0] = _Session(vault=_Vault([shared]))
    cfg = _cfg([{"ref": shared}])

    assert LoraStackConflictCheck().run(cfg).passed is True


def test_an_explicit_cli_stack_overrides_a_diverging_vault(
    session: list[_Session | None],
) -> None:
    """Negative control — CLI wins entirely, by design (P3-D3/D4).

    Bug caught: a check that reads cfg and vault but ignores ``cli_loras``.
    It would reject at preflight a run the resolver would have accepted,
    turning ``--loras`` — the documented override — into an error.
    """
    from kinoforge.core.lora import LoraEntry
    from kinoforge.validation.checks.loras import LoraStackConflictCheck

    session[0] = _Session(
        vault=_Vault([_VAULT_REF]),
        cli_loras=[LoraEntry(ref="civitai:3333@4444")],
    )
    cfg = _cfg([{"ref": "civitai:1111@2222"}])

    assert LoraStackConflictCheck().run(cfg).passed is True


def test_the_conflict_check_runs_before_the_provider_is_touched() -> None:
    """It must be a PREFLIGHT check, or it does not run in time.

    Bug caught: registering it as NETWORK or leaving it out of the
    generate-time categories. ``validate_for_generate`` runs PREFLIGHT +
    NETWORK before ``generate()``, and that ordering is the entire value of
    this item — a check in the wrong category still fires after the boot.
    """
    from kinoforge.validation.checks.loras import LoraStackConflictCheck

    assert LoraStackConflictCheck().category is CheckCategory.PREFLIGHT


# ---------------------------------------------------------------------------
# LoraRefsResolvableCheck
# ---------------------------------------------------------------------------


def test_a_ref_resolving_to_zero_artifacts_fails_before_any_spend(
    monkeypatch: pytest.MonkeyPatch, session: list[_Session | None]
) -> None:
    """The zero-artifact ValidationError, moved ahead of ``create_instance``.

    Bug caught: leaving it inside ``resolve_download_specs``, first reached
    from ``ensure_lora_stack`` after the pod is ready. A ref that resolves to
    nothing is knowable from the ref and credentials alone.
    """
    from kinoforge.core import registry
    from kinoforge.validation.checks.loras import LoraRefsResolvableCheck

    class _EmptySource:
        scheme = "civitai"

        def resolve(self, ref: str, creds: Any) -> list[Any]:
            return []

    monkeypatch.setattr(registry, "source_for_ref", lambda _r: _EmptySource())
    session[0] = _Session()
    cfg = _cfg([{"ref": "civitai:1111@2222"}])

    result = LoraRefsResolvableCheck().run(cfg)

    assert result.passed is False
    assert result.severity is Severity.ERROR


def test_an_unhandled_ref_scheme_fails_before_any_spend(
    monkeypatch: pytest.MonkeyPatch, session: list[_Session | None]
) -> None:
    """UnknownAdapter, moved ahead of ``create_instance``.

    Bug caught: catching only ``ValidationError`` in the new check and
    letting ``UnknownAdapter`` escape to the post-boot path it came from —
    two of the three classes fixed, the third still costing a boot.
    """
    from kinoforge.core import registry
    from kinoforge.core.errors import UnknownAdapter
    from kinoforge.validation.checks.loras import LoraRefsResolvableCheck

    def _boom(_ref: str) -> Any:
        raise UnknownAdapter("no source handles this ref")

    monkeypatch.setattr(registry, "source_for_ref", _boom)
    session[0] = _Session()
    cfg = _cfg([{"ref": "weirdscheme:1111"}])

    result = LoraRefsResolvableCheck().run(cfg)

    assert result.passed is False
    assert result.severity is Severity.ERROR


def test_the_resolvable_check_message_never_names_a_ref(
    monkeypatch: pytest.MonkeyPatch, session: list[_Session | None]
) -> None:
    """Same privacy rule as ``resolve_download_specs``: position, never the ref.

    Bug caught: interpolating the ref into the message. The stack may come
    from a vault, and this message reaches stderr.
    """
    from kinoforge.core import registry
    from kinoforge.validation.checks.loras import LoraRefsResolvableCheck

    class _EmptySource:
        scheme = "civitai"

        def resolve(self, ref: str, creds: Any) -> list[Any]:
            return []

    monkeypatch.setattr(registry, "source_for_ref", lambda _r: _EmptySource())
    session[0] = _Session()
    secret = "civitai:5555@6666"
    cfg = _cfg([{"ref": secret}])

    result = LoraRefsResolvableCheck().run(cfg)

    assert secret not in result.message


def test_a_resolvable_ref_passes(
    monkeypatch: pytest.MonkeyPatch, session: list[_Session | None]
) -> None:
    """Negative control — a healthy stack must not be blocked.

    Bug caught: a check that fails closed on every stack, which would make
    every LoRA run impossible and be reverted rather than fixed.
    """
    from kinoforge.core import registry
    from kinoforge.validation.checks.loras import LoraRefsResolvableCheck

    class _Artifact:
        url = "https://example.invalid/a.safetensors"
        headers: dict[str, str] = {}
        filename = "a.safetensors"
        size = 123

    class _GoodSource:
        scheme = "civitai"

        def resolve(self, ref: str, creds: Any) -> list[Any]:
            return [_Artifact()]

    monkeypatch.setattr(registry, "source_for_ref", lambda _r: _GoodSource())
    session[0] = _Session()
    cfg = _cfg([{"ref": "civitai:1111@2222"}])

    assert LoraRefsResolvableCheck().run(cfg).passed is True


def test_a_transient_source_failure_does_not_block_the_run(
    monkeypatch: pytest.MonkeyPatch, session: list[_Session | None]
) -> None:
    """A network fault is not proof the ref is bad.

    Bug caught: letting any exception from ``source.resolve`` fail the
    check. A flaky Hub or an expired token would then block a run whose
    stack is perfectly valid — converting a retryable condition into a hard
    preflight failure. Only the two DETERMINISTIC classes this item is about
    (zero artifacts, unhandled scheme) may fail here; anything else is
    uncertainty, and uncertainty defers to the post-boot apply, which still
    fails loudly per D7.
    """
    from kinoforge.core import registry
    from kinoforge.validation.checks.loras import LoraRefsResolvableCheck

    class _FlakySource:
        scheme = "civitai"

        def resolve(self, ref: str, creds: Any) -> list[Any]:
            raise TimeoutError("hub unreachable")

    monkeypatch.setattr(registry, "source_for_ref", lambda _r: _FlakySource())
    session[0] = _Session()
    cfg = _cfg([{"ref": "civitai:1111@2222"}])

    assert LoraRefsResolvableCheck().run(cfg).passed is True


def test_the_resolvable_check_is_a_network_check() -> None:
    """It performs I/O, so it must be categorised as such.

    Bug caught: registering it STATIC, which would make every ``load_config``
    — including offline ones, and ``--skip-preflight`` runs — hit the network
    to resolve LoRA refs.
    """
    from kinoforge.validation.checks.loras import LoraRefsResolvableCheck

    assert LoraRefsResolvableCheck().category is CheckCategory.NETWORK


def test_neither_check_applies_to_a_cfg_with_no_loras(
    session: list[_Session | None],
) -> None:
    """No LoRAs, no work — and above all no network call.

    Bug caught: an ``applies_to`` that returns True unconditionally, adding a
    ref-resolution round trip to the preflight of every run in the project.
    """
    from kinoforge.validation.checks.loras import (
        LoraRefsResolvableCheck,
        LoraStackConflictCheck,
    )

    session[0] = _Session()
    cfg = _cfg([])

    assert LoraStackConflictCheck().applies_to(cfg) is False
    assert LoraRefsResolvableCheck().applies_to(cfg) is False
