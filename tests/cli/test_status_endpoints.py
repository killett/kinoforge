"""Behavior: status reads; it does not build.

The rule this pins: no observational command may create a resource. A
tunnel-ensuring ``kinoforge status`` would spawn one ssh subprocess per
invocation and fail offline — see the S5 plan, "four things the design gets
wrong", item 1.
"""

from __future__ import annotations

from typing import Any

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

    Since U3, ``_cmd_status`` rehydrates the ``ports`` tag from the ledger, so
    the case this pins is narrower than when it was written: a row that records
    no tags and no endpoints either — a legacy or truncated row, or a pod whose
    create never got that far. The empty map still has to keep looking wrong.

    Bug caught: ``_cmd_status`` builds its ``Instance`` from a bare
    ``provider.get_instance()``, which drops RunPod's ``ports`` tag, so
    ``RunPodProvider.endpoints`` returns ``{}`` on the status path whenever the
    ledger has no tags to restore — not a hypothetical. Painting that empty map
    as ``cluster=<pod-id>`` would borrow skypilot's semantics (the id IS the cluster name, and
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


def test_status_labels_a_recorded_endpoint_map_it_could_not_confirm() -> None:
    """A ledger-held URL is shown, and shown as unverified (U3).

    ``ModalProvider.get_instance`` builds its ``Instance`` from
    ``modal app list``, which carries no URL, and ``endpoints()`` then falls
    back to a per-process ``_deployments`` dict a fresh CLI process never
    populated. The ledger DOES hold the URL, so status had every means to
    answer and still printed ``unknown (no live endpoint)``.

    Bug caught, two of them in one assertion:
      * U3's first reproducer — ``kinoforge status --id`` on a live Modal pod
        says ``unknown`` while the recorded ``.modal.run`` URL is answering
        ``GET /util``, which is what forced the 2026-09-06 matrix run to read
        ``ledger.json`` by hand to poll a pod at all.
      * the inverse — rendering it BARE, as if verified. ``endpoints()`` is
        the pure read; on skypilot the recorded endpoint is routinely a
        ``127.0.0.1:<port>`` tunnel that died with the process that opened
        it, and presenting that as live is the F11 failure the S5 read/ensure
        split exists to prevent.
    """
    from kinoforge.cli._commands import _render_endpoints_for_status

    class _ModalNoLiveMap(_SpyProvider):
        name = "modal"

        def endpoints(self, instance: Instance) -> dict[str, str]:
            self.calls.append("endpoints")
            return {}

    inst = Instance(
        id="run-20260909-011500",
        provider="modal",
        status="ready",
        created_at=0.0,
        endpoints={},
        tags={},
        cost_rate_usd_per_hr=0.0,
    )
    provider = _ModalNoLiveMap()

    rendered = _render_endpoints_for_status(
        provider,
        inst,
        recorded={"8000": "https://kinoforge-run-8000.modal.run"},
    )

    # Still the pure read — a recorded map must not tempt the repairing door.
    assert provider.calls == ["endpoints"]
    assert rendered == (
        '{"8000": "https://kinoforge-run-8000.modal.run"} '
        "(recorded at launch, not verified live)"
    )


def test_status_prefers_the_live_endpoint_map_over_the_recorded_one() -> None:
    """When the provider answers, the ledger's copy is ignored.

    Bug caught: a precedence inversion that pins the status line to the
    launch-time URL. An operator debugging a pod whose endpoint moved would
    be handed the stale one by the command whose whole job is to report
    current state — and it would look authoritative, because the recorded
    map renders without the unverified label once precedence is wrong.
    """
    from kinoforge.cli._commands import _render_endpoints_for_status

    class _ModalLive(_SpyProvider):
        name = "modal"

        def endpoints(self, instance: Instance) -> dict[str, str]:
            self.calls.append("endpoints")
            return {"8000": "https://current.modal.run"}

    inst = Instance(
        id="run-1",
        provider="modal",
        status="ready",
        created_at=0.0,
        endpoints={},
        tags={},
        cost_rate_usd_per_hr=0.0,
    )

    rendered = _render_endpoints_for_status(
        _ModalLive(), inst, recorded={"8000": "https://stale.modal.run"}
    )

    assert rendered == '{"8000": "https://current.modal.run"}'
    assert "stale" not in rendered
    assert "recorded" not in rendered


def test_status_renders_runpod_proxy_urls_from_rehydrated_port_tags() -> None:
    """Rehydrated ``ports`` tags let RunPod DERIVE its URLs — so no label.

    This is the piece ``_render_endpoints_for_status``'s own docstring
    deferred as "a separate, deferred piece of work — out of scope here".

    The distinction the assertion pins: a recorded ``ports`` tag is not a
    recollection of a URL, it is an input from which
    ``RunPodProvider.endpoints`` COMPUTES the proxy URL, deterministically
    from the pod id. So it renders unlabelled, unlike a recorded endpoint
    literal. Uses the real provider rather than a fake precisely because a
    fake would prove nothing about that computation.

    Bug caught: ``status`` reporting ``unknown (no live endpoint)`` for a
    perfectly healthy RunPod pod, which reads as "something is wrong with
    the pod" when the truth is "the status path forgot to pass the ports
    along".
    """
    from kinoforge.cli._commands import (
        _merge_recorded_tags,
        _render_endpoints_for_status,
    )
    from kinoforge.providers.runpod import RunPodProvider

    # Exactly what `RunPodProvider.get_instance` yields: the pod-query
    # selection set carries no port spec, so `tags` arrives empty.
    inst = Instance(
        id="kfpod123",
        provider="runpod",
        status="ready",
        created_at=0.0,
        endpoints={},
        tags={},
        cost_rate_usd_per_hr=0.0,
    )
    entry: dict[str, Any] = {
        "id": "kfpod123",
        "tags": {"ports": "8000,8001", "mode": "pod"},
    }

    _merge_recorded_tags(inst, entry)
    rendered = _render_endpoints_for_status(
        RunPodProvider(), inst, recorded=entry.get("endpoints")
    )

    assert rendered == (
        '{"8000": "https://kfpod123-8000.proxy.runpod.net", '
        '"8001": "https://kfpod123-8001.proxy.runpod.net"}'
    )
    assert "recorded" not in rendered
