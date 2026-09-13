"""The boot-stall grace must respect the cfg's declared ``boot_timeout``.

Found live 2026-09-12 running the shipped 1.3B swap grid, twice (pods
``rohjrsmre9obsp`` and ``e7wo3ffamgiqln``). That cfg declares
``boot_timeout: 30m``; the probe aborted it after ``grace 90 s + 3 x 30 s`` —
about **3 minutes**, one-tenth of the boot budget the config asked for — while
the pod was still fetching model weights.

The flat 90 s grace was calibrated for pods whose work shows up in CPU. A Wan
1.3B cold boot spends minutes in an HF download that is CPU-idle (network-bound)
and memory-flat (it streams to disk), and RunPod reports no ``disk_percent`` at
all, so the util signals cannot distinguish it from a dead container. U40's
log-progress signal was supposed to cover that gap and proved INERT here: the
port-8001 sidecar is not proxy-reachable during early boot — two fetches during
the flatline returned 0 bytes — which is precisely when the flatline happens.

So the remaining honest signal is the operator's own declaration. A config that
says "my boot takes up to 30 minutes" should not be killed at minute three. The
fast-fail is kept, just scaled to what the config claimed.
"""

from __future__ import annotations

from kinoforge.core.interfaces import Instance
from kinoforge.providers.runpod import RunPodProvider, boot_grace_seconds


class TestBootGraceSeconds:
    """Grace is derived from the declared boot timeout, within bounds."""

    def test_long_boot_timeout_earns_proportional_grace(self) -> None:
        """A 30 m boot budget yields far more than the flat 90 s.

        Bug caught: the shipped behaviour that killed pods rohjrsmre9obsp and
        e7wo3ffamgiqln ~3 min into a boot their own cfg budgeted 30 min for.
        """
        assert boot_grace_seconds(1800.0) == 450.0

    def test_short_boot_timeout_does_not_fall_below_the_floor(self) -> None:
        """A tiny budget still gets the original 90 s.

        Bug caught: scaling naively, so a cfg with ``boot_timeout: 2m`` gets a
        30 s grace and starts false-killing pods that the flat 90 s handled
        correctly. The change must not make any existing case WORSE.
        """
        assert boot_grace_seconds(120.0) == 90.0

    def test_absurd_boot_timeout_is_capped(self) -> None:
        """Grace is bounded, so fast-fail cannot be disabled by a huge budget.

        Bug caught: an unbounded derivation — ``boot_timeout: 24h`` would buy a
        6-hour grace, silently turning the stall guard off and restoring the
        exact 40-minute-dead-pod bill the probe exists to prevent.
        """
        assert boot_grace_seconds(86400.0) == 900.0

    def test_missing_boot_timeout_keeps_the_documented_default(self) -> None:
        """``None`` behaves exactly as before this change.

        Bug caught: a provider or call path that does not supply the timeout
        getting 0 s of grace and killing every pod during its first probe.
        """
        assert boot_grace_seconds(None) == 90.0


class TestProbeWiring:
    """The provider must actually pass the derived value through."""

    def test_probe_is_built_with_the_derived_grace(self) -> None:
        """``make_boot_liveness_probe`` honours the boot timeout it is given.

        This is the load-bearing test. ``boot_grace_seconds`` can be perfectly
        correct and change nothing if the factory ignores it — which is exactly
        how U40 failed: right logic, never reached in production. Asserting on
        the constructed probe's own state is what proves the wiring.
        """
        prov = RunPodProvider()
        prov._util_endpoint = object()  # type: ignore[assignment]
        inst = _instance()

        probe = prov.make_boot_liveness_probe(inst, boot_timeout_s=1800.0)

        assert probe is not None
        assert probe._grace_s == 450.0, probe._grace_s

    def test_probe_without_a_timeout_keeps_the_default(self) -> None:
        """Omitting the argument preserves the pre-existing 90 s contract.

        Bug caught: making the parameter required, which would break every
        existing caller and test that builds a probe without one.
        """
        prov = RunPodProvider()
        prov._util_endpoint = object()  # type: ignore[assignment]

        probe = prov.make_boot_liveness_probe(_instance())

        assert probe is not None
        assert probe._grace_s == 90.0, probe._grace_s


def _instance() -> Instance:
    """Return a minimal Instance for probe construction.

    Returns:
        An ``Instance`` carrying only the fields the factory reads.
    """
    return Instance(
        id="pod-deadbeef",
        provider="runpod",
        status="running",
        created_at=1757627830.0,
    )


class TestOrchestratorPassesTheTimeout:
    """The orchestrator must actually hand the cfg's budget to the factory.

    Written after a falsification exposed that it was NOT covered: deleting
    ``boot_timeout_s=lifecycle.boot_timeout_s`` from the orchestrator left all
    23 boot-liveness tests green. That is precisely how U40 shipped inert —
    correct logic, never reached in production — so the seam itself is pinned
    here rather than only the helper it calls.
    """

    def test_the_declared_budget_reaches_make_boot_liveness_probe(self) -> None:
        """The factory receives the lifecycle's boot_timeout_s, not nothing.

        Bug caught: the orchestrator calling ``_make_probe(instance)`` and
        silently taking the 90 s default, which reproduces the exact false-kill
        this change exists to stop while every unit test stays green.
        """
        import inspect

        from kinoforge.core import orchestrator

        src = inspect.getsource(orchestrator._provision_instance_and_build_backend)
        call = src[src.index("make_boot_liveness_probe") :]

        assert "boot_timeout_s=lifecycle.boot_timeout_s" in call, (
            "the orchestrator must pass the cfg's declared boot budget to the "
            "probe factory; without it the derived grace is unreachable"
        )

    def test_a_factory_without_the_parameter_still_works(self) -> None:
        """A provider whose factory predates the parameter is not broken.

        Bug caught: hard-passing the kwarg and crashing every third-party or
        older provider whose ``make_boot_liveness_probe`` takes only the
        instance — a TypeError during provisioning, on a pod already billing.
        """
        import inspect

        from kinoforge.core import orchestrator

        src = inspect.getsource(orchestrator._provision_instance_and_build_backend)

        assert "except TypeError" in src, (
            "the call must fall back for factories without boot_timeout_s"
        )
