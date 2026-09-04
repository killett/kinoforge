"""Behavior: status reads; it does not build.

The rule this pins: no observational command may create a resource. A
tunnel-ensuring ``kinoforge status`` would spawn one ssh subprocess per
invocation and fail offline — see the S5 plan, "four things the design gets
wrong", item 1.
"""

from __future__ import annotations

from kinoforge.core.interfaces import Instance


class _SpyProvider:
    """Records which endpoint door was used."""

    name = "skypilot"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def endpoints(self, instance: Instance) -> dict[str, str]:
        self.calls.append("endpoints")
        return {}

    def ensure_endpoints(self, instance: Instance) -> dict[str, str]:
        self.calls.append("ensure_endpoints")
        return {"8000": "http://127.0.0.1:50001"}


def test_status_uses_the_pure_read_and_names_the_cluster() -> None:
    """Status never repairs, and says what it can identify.

    Bug caught: printing ``{}`` for a live cluster tells the operator nothing,
    and printing ``ssh://<name>`` tells them something an HTTP client cannot
    use (finding F11).
    """
    from kinoforge.cli._commands import _render_endpoints_for_status

    provider = _SpyProvider()
    inst = Instance(
        id="kf-cluster-7",
        provider="skypilot",
        status="ready",
        created_at=0.0,
        endpoints={},
        tags={},
        cost_rate_usd_per_hr=0.0,
    )
    rendered = _render_endpoints_for_status(provider, inst)
    assert provider.calls == ["endpoints"]
    # The exact form, as its sibling below asserts the exact "unknown" form: a
    # substring check passes for ``unknown (kf-cluster-7)`` or for a JSON blob
    # that merely happens to contain the id, neither of which is the
    # ``cluster=<id>`` string an operator is meant to paste into
    # ``kinoforge destroy --id`` or ``sky status``.
    assert rendered == "cluster=kf-cluster-7"


def test_status_renders_a_real_endpoint_map_when_there_is_one() -> None:
    """A provider with endpoints still gets its JSON map.

    Bug caught: the skypilot special case swallowing RunPod's proxy URLs.
    """
    from kinoforge.cli._commands import _render_endpoints_for_status

    class _RunPodish(_SpyProvider):
        name = "runpod"

        def endpoints(self, instance: Instance) -> dict[str, str]:
            self.calls.append("endpoints")
            return {"8000": "https://abc-8000.proxy.runpod.net"}

    inst = Instance(
        id="abc",
        provider="runpod",
        status="ready",
        created_at=0.0,
        endpoints={},
        tags={},
        cost_rate_usd_per_hr=0.0,
    )
    assert "proxy.runpod.net" in _render_endpoints_for_status(_RunPodish(), inst)


def test_status_never_names_a_runpod_pod_as_a_cluster() -> None:
    """A non-skypilot provider with no live endpoint says "unknown", not "cluster=".

    Bug caught: ``_cmd_status`` builds its ``Instance`` from a bare
    ``provider.get_instance()``, which drops RunPod's ``ports`` tag, so
    ``RunPodProvider.endpoints`` genuinely returns ``{}`` on the status path
    — not a hypothetical. Painting that empty map as ``cluster=<pod-id>``
    would borrow skypilot's semantics (the id IS the cluster name, and
    ``sky status``/``kinoforge destroy --id`` are the honest follow-ups) for
    a provider where that isn't true, hiding a real "something is wrong"
    signal behind a benign-looking cluster label.
    """
    from kinoforge.cli._commands import _render_endpoints_for_status

    class _RunPodEmpty(_SpyProvider):
        name = "runpod"

        def endpoints(self, instance: Instance) -> dict[str, str]:
            self.calls.append("endpoints")
            return {}

    inst = Instance(
        id="abc",
        provider="runpod",
        status="ready",
        created_at=0.0,
        endpoints={},
        tags={},
        cost_rate_usd_per_hr=0.0,
    )
    rendered = _render_endpoints_for_status(_RunPodEmpty(), inst)
    assert rendered == "unknown (no live endpoint)"
    assert "cluster=" not in rendered
