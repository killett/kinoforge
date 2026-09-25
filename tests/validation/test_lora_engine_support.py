"""Behavior: an engine that cannot apply a LoRA stack refuses one at validation.

U56. A hosted-engine config carrying ``loras:`` generated LoRA-less video. Task
7 of the H3 LoRA seam build made that a WARNING rather than silence, but a
WARNING in a multi-minute boot log is not a gate, and the run still produces a
plausible video with no LoRA in it — discovered by eye, if at all.

**Why this could not be another cfg-load special case**, which is why the
filing left it open. Two shapes look identical to a naive check and must be
told apart:

* ``--loras`` never enters ``cfg.loras`` — it lives on the ambient
  ``EphemeralSession`` — so a check reading only the cfg misses the CLI path.
* A ComfyUI config LEGITIMATELY carries ``loras:``. It feeds
  ``capability_key()`` while the adapters are applied through workflow NODES,
  never through the ``set_lora_stack`` seam. A gate keyed on
  ``engine.kind != "diffusers"`` would reject configs that are correct — and
  ``LoraServerSupportCheck`` declines to apply outside diffusers for exactly
  this reason.

So the gate has to be a per-engine CAPABILITY declaration, parallel to
``Capability.RATE_READBACK`` on ``ComputeProvider``: the engine says whether it
can apply a stack and by what route, and the check reads that rather than
pattern-matching on ``engine.kind``.

The check is STATIC, which under ``_run_gated`` runs BOTH at ``load_config``
and again inside ``validate_for_generate`` — so the cfg-declared case is caught
at load (the earliest possible point) and the CLI case is caught at preflight,
once the session exists. Reading the ambient session is not I/O, so the
category's no-I/O contract holds.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.lora_capability import LoraSupport
from kinoforge.validation.protocol import CheckCategory, Severity


def _cfg(engine: dict[str, Any], loras: list[dict[str, Any]]) -> Any:
    from kinoforge.core.config import Config

    return Config.model_validate(
        {
            "mode": "t2v",
            "engine": engine,
            "models": [
                {"ref": "hf:org/repo", "kind": "base", "target": "diffusion_models"}
            ],
            "loras": loras,
        }
    )


def _hosted_cfg(loras: list[dict[str, Any]]) -> Any:
    return _cfg(
        {
            "kind": "hosted",
            "precision": "",
            "hosted": {
                "provider": "my-shim",
                "endpoint": "https://shim.example.invalid/inference",
                "api_key_env": "MY_SHIM_KEY",
                "url_path": "video.url",
            },
        },
        loras,
    )


def _diffusers_cfg(loras: list[dict[str, Any]]) -> Any:
    return _cfg(
        {
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
        loras,
    )


class _Session:
    def __init__(self, cli_loras: Any = None) -> None:
        self.vault = None
        self.cli_loras = cli_loras


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[_Session | None]]:
    holder: list[_Session | None] = [None]
    monkeypatch.setattr(
        EphemeralSession, "current", staticmethod(lambda: holder[0]), raising=False
    )
    yield holder


# ---------------------------------------------------------------------------
# The declaration itself
# ---------------------------------------------------------------------------


def test_every_registered_engine_declares_its_lora_support() -> None:
    """A new engine must not default into "LoRAs are fine here".

    Bug caught: adding an engine and inheriting a permissive default. The
    whole failure mode U56 describes is an engine silently accepting a stack
    it cannot apply, so the declaration has to be required rather than
    defaulted — the same reasoning as
    ``test_every_provider_declares_its_nothing_booked_errors``.
    """
    import kinoforge.engines.bedrock_video  # noqa: F401
    import kinoforge.engines.comfyui  # noqa: F401
    import kinoforge.engines.diffusers  # noqa: F401
    import kinoforge.engines.fake  # noqa: F401
    import kinoforge.engines.fal  # noqa: F401
    import kinoforge.engines.hosted  # noqa: F401
    from kinoforge.core import registry
    from kinoforge.core.interfaces import GenerationEngine

    for name in registry.engine_names():
        engine = registry.get_engine(name)()
        # Scope to engines this package SHIPS. The registry is a mutable
        # global and other tests register doubles into it, so iterating it
        # raw makes this assertion order-dependent — and a test-local double
        # has no obligation to answer a question about real LoRA routing.
        if not type(engine).__module__.startswith("kinoforge.engines."):
            continue
        assert isinstance(engine.lora_support(), LoraSupport)
        # Overriding, not inheriting. The ABC's default is deliberately the
        # noisy one, so inheriting it is survivable rather than silent — but
        # it is still an engine that never answered the question, and the
        # answer is not guessable from outside the engine. Compared through
        # `__func__` because both are classmethods; the factory is a lambda,
        # so the class has to come from the instance.
        assert (
            type(engine).lora_support.__func__  # type: ignore[attr-defined]
            is not GenerationEngine.lora_support.__func__  # type: ignore[attr-defined]
        ), (
            f"engine {name!r} inherits lora_support() instead of declaring "
            f"one; the answer is not guessable from outside the engine (U56)"
        )


def test_diffusers_declares_the_http_seam() -> None:
    """Diffusers applies stacks over ``/lora/set_stack``.

    Bug caught: declaring NONE for diffusers, which would make the new check
    reject every working LoRA config in the repo.
    """
    from kinoforge.engines.diffusers import DiffusersEngine

    assert DiffusersEngine.lora_support() is LoraSupport.SERVER_HTTP


def test_comfyui_declares_workflow_application_not_none() -> None:
    """ComfyUI applies adapters through graph NODES, not through this seam.

    Bug caught: declaring NONE for ComfyUI because it has no
    ``set_lora_stack``. Its ``loras:`` block is legitimate — it feeds
    ``capability_key()`` — so NONE would reject correct configs, which is the
    precise mistake the filing warned a naive gate would make.
    """
    from kinoforge.engines.comfyui import ComfyUIEngine

    assert ComfyUIEngine.lora_support() is LoraSupport.WORKFLOW


def test_hosted_declares_no_lora_support() -> None:
    """The engine the defect was found on.

    Bug caught: leaving hosted permissive, which is U56 unfixed.
    """
    from kinoforge.engines.hosted import HostedAPIEngine

    assert HostedAPIEngine.lora_support() is LoraSupport.NONE


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def test_a_hosted_cfg_with_loras_is_refused(session: list[_Session | None]) -> None:
    """The defect. A stack that can never be applied must not reach a run.

    Bug caught: leaving this a WARNING. The run completes, bills, and returns
    a plausible video with no LoRA in it — the failure mode that is only
    caught by eye.
    """
    from kinoforge.validation.checks.loras import LoraEngineSupportCheck

    cfg = _hosted_cfg([{"ref": "civitai:1111@2222"}])
    check = LoraEngineSupportCheck()

    assert check.applies_to(cfg) is True
    result = check.run(cfg)

    assert result.passed is False
    assert result.severity is Severity.ERROR


def test_a_comfyui_cfg_with_loras_is_allowed(session: list[_Session | None]) -> None:
    """Negative control, and the one the filing says a naive gate gets wrong.

    Bug caught: keying the gate on ``engine.kind != "diffusers"``. ComfyUI's
    ``loras:`` block is correct and load-bearing for ``capability_key()``;
    rejecting it would break working configs and the fix would be reverted.
    """
    from kinoforge.validation.checks.loras import LoraEngineSupportCheck

    cfg = _cfg(
        {
            "kind": "comfyui",
            "precision": "fp16",
            "comfyui": {"version": "0.3.10"},
        },
        [{"ref": "civitai:1111@2222"}],
    )

    assert LoraEngineSupportCheck().run(cfg).passed is True


def test_a_diffusers_cfg_with_loras_is_allowed(session: list[_Session | None]) -> None:
    """Negative control — the supported path stays open.

    Bug caught: a check that fails closed for every engine.
    """
    from kinoforge.validation.checks.loras import LoraEngineSupportCheck

    assert LoraEngineSupportCheck().run(_diffusers_cfg([{"ref": "c:1@2"}])).passed


def test_a_cli_stack_on_a_hosted_engine_is_refused(
    session: list[_Session | None],
) -> None:
    """``--loras`` never enters ``cfg.loras``; the check must still see it.

    Bug caught: reading only ``cfg.loras``. The filing names this as the
    reason a plain cfg-load check cannot close U56 — a CLI stack on a hosted
    engine is exactly as undeliverable as a cfg one, and exactly as silent.
    """
    from kinoforge.core.lora import LoraEntry
    from kinoforge.validation.checks.loras import LoraEngineSupportCheck

    session[0] = _Session(cli_loras=[LoraEntry(ref="civitai:3333@4444")])
    cfg = _hosted_cfg([])
    check = LoraEngineSupportCheck()

    assert check.applies_to(cfg) is True
    assert check.run(cfg).passed is False


def test_an_explicit_empty_cli_stack_is_not_refused(
    session: list[_Session | None],
) -> None:
    """``--loras ""`` is a request to hold NOTHING — never an error.

    Bug caught: treating ``cli_loras == []`` as "a stack was requested".
    ``[]`` means clear, and refusing it would break ``kinoforge grid``'s
    control cell, which emits exactly that to guarantee a LoRA-less baseline.
    """
    from kinoforge.validation.checks.loras import LoraEngineSupportCheck

    session[0] = _Session(cli_loras=[])
    cfg = _hosted_cfg([])

    assert LoraEngineSupportCheck().run(cfg).passed is True


def test_the_message_never_names_a_ref(session: list[_Session | None]) -> None:
    """Same privacy rule as every other LoRA surface: counts, never refs.

    Bug caught: interpolating the stack into the message. It reaches stderr,
    and the stack may have come from a vault.
    """
    from kinoforge.validation.checks.loras import LoraEngineSupportCheck

    secret = "civitai:7777@8888"
    result = LoraEngineSupportCheck().run(_hosted_cfg([{"ref": secret}]))

    assert secret not in result.message


def test_the_check_is_static_so_it_fires_at_config_load(
    session: list[_Session | None],
) -> None:
    """STATIC runs at ``load_config`` AND again inside ``validate_for_generate``.

    Bug caught: making it PREFLIGHT. The cfg-declared case would then be
    caught only at generate time, losing the earliest and cheapest signal —
    and ``kinoforge doctor`` reporting on a cfg that cannot work.
    """
    from kinoforge.validation.checks.loras import LoraEngineSupportCheck

    assert LoraEngineSupportCheck().category is CheckCategory.STATIC


def test_the_check_does_not_apply_without_a_stack(
    session: list[_Session | None],
) -> None:
    """No stack from any source, nothing to say.

    Bug caught: an ``applies_to`` that fires on every hosted cfg, making the
    check noise on runs that never mentioned LoRAs.
    """
    from kinoforge.validation.checks.loras import LoraEngineSupportCheck

    assert LoraEngineSupportCheck().applies_to(_hosted_cfg([])) is False


def test_the_apply_seam_does_not_warn_for_a_workflow_engine(
    session: list[_Session | None], caplog: pytest.LogCaptureFixture
) -> None:
    """The other half of U56's fix direction: gate the WARNING too.

    ``ensure_lora_stack`` WARNs "NOT APPLYING N LoRA entries" whenever the
    backend has no ``set_lora_stack``. ComfyUI's has none and never will —
    its adapters go through workflow nodes — so that warning fires on every
    correct ComfyUI LoRA run.

    Bug caught: leaving it ungated. The seam's own code comment says a
    warning has to "earn its place in a multi-minute boot log"; one that
    fires on a healthy configuration is how a load-bearing alarm gets
    trained out of its reader, which is the same reasoning that gave
    ``--loras ""`` an INFO instead of the discard WARNING.
    """
    import logging

    from kinoforge.core.lora_apply import ensure_lora_stack

    cfg = _cfg(
        {"kind": "comfyui", "precision": "fp16", "comfyui": {"version": "0.3.10"}},
        [{"ref": "civitai:1111@2222"}],
    )

    with caplog.at_level(logging.INFO, logger="kinoforge.core.lora_apply"):
        ensure_lora_stack(
            backend=object(), cfg=cfg, pod_id="pod-1", creds=None, ledger=None
        )

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        f"a WORKFLOW engine must not warn about not applying: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


def test_the_apply_seam_still_warns_for_an_engine_that_should_have_applied(
    session: list[_Session | None], caplog: pytest.LogCaptureFixture
) -> None:
    """Negative control — the real alarm must survive.

    Bug caught: silencing the warning for every engine. On diffusers a
    backend with no ``set_lora_stack`` IS a genuine discrepancy — the engine
    declares SERVER_HTTP, so a stack was supposed to be applied and was not.
    """
    import logging

    from kinoforge.core.lora_apply import ensure_lora_stack

    cfg = _diffusers_cfg([{"ref": "civitai:1111@2222"}])

    with caplog.at_level(logging.INFO, logger="kinoforge.core.lora_apply"):
        ensure_lora_stack(
            backend=object(), cfg=cfg, pod_id="pod-1", creds=None, ledger=None
        )

    assert [r for r in caplog.records if r.levelno >= logging.WARNING]
