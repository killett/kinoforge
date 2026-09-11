"""Behavior: ``deploy()`` renders the engine's provision like every other path.

``kinoforge deploy`` built its ``InstanceSpec`` from a hard-coded EMPTY
``RenderedProvision(script="", image=image, ports=[], env_required=[])`` and
never called ``engine.render_provision`` at all — unlike ``deploy_session``
(the ``generate`` path) and ``_cmd_provision``. The resulting spec had no setup
steps, no launch command, no ports and no env.

What that cost, per U19's sibling entry U6: on Modal the provider refuses with
an uncaught ``ValueError`` (the right behaviour arriving as a traceback), and on
RunPod the same empty spec books a pod with no ports and no bootstrap — the
documented ``forewgeluuy9qh`` money-loss shape, where ``wait_for_ready`` raises
``ProvisionFailed: ... has no endpoints`` only AFTER the pod is billing.

``creds`` is part of the same defect. ``deploy()`` declared the parameter and
documented it as defaulting to ``EnvCredentialProvider()``, then never read it:
a safety promise present in the signature and absent in effect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

# Import providers/engines/sources so they self-register for deploy().
import kinoforge.engines.fake  # noqa: F401
import kinoforge.providers.local  # noqa: F401
import kinoforge.sources.http  # noqa: F401 — registers https:// source
from kinoforge.core.errors import AuthError
from kinoforge.core.interfaces import Instance, InstanceSpec
from kinoforge.core.orchestrator import deploy
from kinoforge.providers.local import LocalProvider
from tests.core.test_orchestrator import _compute_cfg, _make_engine

if TYPE_CHECKING:
    from pathlib import Path


class _SpecCapturingProvider(LocalProvider):
    """Records the spec handed to ``create_instance``, then creates normally."""

    def __init__(self) -> None:
        super().__init__()
        self.specs: list[InstanceSpec] = []

    def create_instance(self, spec: InstanceSpec) -> Instance:
        """Capture *spec*, then delegate to the real local provider.

        Args:
            spec: The spec the orchestrator built.

        Returns:
            The created instance.
        """
        self.specs.append(spec)
        return super().create_instance(spec)


class _RefusingProvider(LocalProvider):
    """Fails the test if ``create_instance`` is reached at all."""

    def create_instance(self, spec: InstanceSpec) -> Instance:
        """Raise — reaching here means money was about to be spent.

        Args:
            spec: Unused.

        Raises:
            AssertionError: Always.
        """
        raise AssertionError(
            "create_instance reached: the pre-create validation deploy() is "
            "supposed to run did not run, so a failure that costs nothing "
            "would have been discovered only after the pod was billing"
        )


class _EnvRequiringEngine:
    """Wraps FakeEngine, adding a required env var to the rendered provision."""

    def __init__(self, var: str) -> None:
        self._inner = _make_engine()
        self._var = var

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 — delegation
        return getattr(self._inner, name)

    def render_provision(self, cfg: dict[str, object]) -> Any:  # noqa: ANN401
        """Return the inner engine's provision with one env var demanded.

        Args:
            cfg: Loaded cfg dict, forwarded verbatim.

        Returns:
            The inner ``RenderedProvision`` with ``env_required`` set.
        """
        import dataclasses

        return dataclasses.replace(
            self._inner.render_provision(cfg), env_required=[self._var]
        )


class _FixedCreds:
    """Credential provider returning one known value, or None for anything else."""

    def __init__(self, var: str, value: str | None) -> None:
        self._var = var
        self._value = value

    def get(self, name: str) -> str | None:
        """Return the configured value for the configured var.

        Args:
            name: Environment variable name.

        Returns:
            The value, or ``None``.
        """
        return self._value if name == self._var else None


def test_deploy_puts_the_engines_rendered_provision_in_the_spec(
    tmp_path: Path,
) -> None:
    """U6: the spec carries setup steps, a launch and ports — not blanks.

    Bug caught: the hard-coded empty ``RenderedProvision``. A spec with no
    launch is refused outright by Modal (as an uncaught ``ValueError``) and
    booked anyway by RunPod, which then bills for a pod that has no server on
    it and no port to reach one through. Asserted against what the engine
    actually rendered rather than literals, so this cannot pass by agreeing
    with a second hard-coded value.
    """
    provider = _SpecCapturingProvider()
    engine = _make_engine()
    cfg = _compute_cfg()
    expected = engine.render_provision(cfg.model_dump())

    deploy(cfg, provider=provider, engine=engine, run_id="deploy-u6")

    assert len(provider.specs) == 1
    spec = provider.specs[0]
    assert spec.setup_steps == expected.setup_steps
    assert spec.launch == expected.launch
    assert list(spec.ports) == list(expected.ports)


def test_deploy_refuses_before_create_when_a_required_env_var_is_missing() -> None:
    """A credential failure must cost nothing, so it happens pre-create.

    Bug caught: ``deploy()`` accepted a ``creds`` provider, documented it as
    defaulting to ``EnvCredentialProvider()``, and never called it. An engine
    whose provision needs ``HF_TOKEN`` would boot a pod that cannot possibly
    work and fail on the wire minutes later, with the pod billing throughout.
    """
    engine = _EnvRequiringEngine("KINOFORGE_TEST_TOKEN")

    with pytest.raises(AuthError, match="KINOFORGE_TEST_TOKEN"):
        deploy(
            _compute_cfg(),
            provider=_RefusingProvider(),
            # Structural fakes: these delegate to FakeEngine / read one var.
            engine=engine,  # type: ignore[arg-type]
            creds=_FixedCreds("KINOFORGE_TEST_TOKEN", None),  # type: ignore[arg-type]
            run_id="deploy-u6-missing-env",
        )


def test_deploy_passes_resolved_credentials_through_to_the_spec() -> None:
    """The resolved value reaches the pod, not just the check.

    Bug caught: rendering ``env_required``, validating it, and then building
    the spec with ``env={}`` anyway — which is what the pre-fix code did
    unconditionally. The pod boots without its token and fails at first use,
    which looks like a model or network fault rather than a wiring one.
    """
    provider = _SpecCapturingProvider()

    deploy(
        _compute_cfg(),
        provider=provider,
        engine=_EnvRequiringEngine("KINOFORGE_TEST_TOKEN"),  # type: ignore[arg-type]
        creds=_FixedCreds("KINOFORGE_TEST_TOKEN", "tok-deadbeef"),  # type: ignore[arg-type]
        run_id="deploy-u6-env",
    )

    assert provider.specs[0].env["KINOFORGE_TEST_TOKEN"] == "tok-deadbeef"


def test_dry_run_still_needs_no_credentials() -> None:
    """``--dry-run`` stays cheap and credential-free.

    Bug caught: hoisting the render + credential resolution above the dry-run
    branch. ``deploy --dry-run`` is the command an operator uses to inspect a
    plan on a machine that may hold no secrets at all; making it raise
    ``AuthError`` would break the one path that is guaranteed to cost nothing.
    """
    result = deploy(
        _compute_cfg(),
        dry_run=True,
        provider=_RefusingProvider(),
        engine=_EnvRequiringEngine("KINOFORGE_TEST_TOKEN"),  # type: ignore[arg-type]
        creds=_FixedCreds("KINOFORGE_TEST_TOKEN", None),  # type: ignore[arg-type]
    )

    assert result.instance is None
    assert result.plan_text is not None
