"""Resolving a reserved ephemeral NAME to the pod id RunPod actually accepts (U16).

``--ephemeral`` mints the pod's name controller-side (spec A2,
:meth:`kinoforge.core.ephemeral.EphemeralSession.resource_name`) so the
pre-create index row can name the resource the create is about to book. On
Modal that closes the whole problem, because the opaque name IS the
``Instance.id``. On RunPod it does not: the create mutation returns RunPod's
OWN id, which cannot exist before the create returns, so for the whole
multi-minute cold boot the only durable handle on a billing pod is a string
neither ``destroy_instance`` nor ``probe_runtime`` accepts.

The consequences are the two that cost money:

* ``kinoforge destroy --id kinoforge-<8hex>`` posts a terminate for a podId
  RunPod has never heard of, then polls, sees ``data.pod = null``, and reports
  the pod CONFIRMED GONE. The operator reads success; the pod keeps billing.
* the sweeper's ``probe_runtime`` asks ``pod(input:{podId: <name>})`` and gets
  ``found=False``, which is indistinguishable from a pod that never existed —
  so the reaper can classify a live, booting pod ``GC_404`` and delete the row
  that was protecting it.

The fix is the shape U17 used for Modal: resolve the identifier the provider
actually accepts. ``tags["name"]`` is already populated by ``_pod_to_instance``
on every listed pod, so the lookup is one existing query.

The resolution is deliberately OFF the critical path. An identifier that
resolves to nothing behaves exactly as it does today, because "not in the
listing" is also what an already-destroyed pod looks like and teardown
idempotency is load-bearing for every ``--no-reuse`` exit.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import TeardownError
from kinoforge.core.util_endpoints import UtilSnapshot
from kinoforge.providers.runpod import RunPodProvider

#: The shape `EphemeralSession.resource_name` mints for RunPod: the prefix plus
#: eight hex. It is the pod's NAME, never its id.
_RESERVED_NAME = "kinoforge-deadbeef"
#: What RunPod's create mutation hands back instead, and the only thing its
#: terminate / pod-detail queries accept.
_REAL_ID = "pod-7yq2m1"


class _Transport:
    """A query-dispatching POST double, so tests state intent, not call order.

    The positional spies elsewhere in this suite index a response list, which
    would make every test here a count of how many queries the implementation
    happens to make. Dispatching on the query text lets a test say "the listing
    holds these pods" and assert on the terminate that actually went out.
    """

    def __init__(
        self,
        *,
        pods: list[dict[str, Any]] | None = None,
        list_raises: Exception | None = None,
    ) -> None:
        self.pods = pods if pods is not None else []
        self.list_raises = list_raises
        self.terminated: list[str] = []
        self.list_calls: int = 0

    def __call__(self, _url: str, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query", ""))
        if "myself { pods" in query:
            self.list_calls += 1
            if self.list_raises is not None:
                raise self.list_raises
            return {"data": {"myself": {"pods": self.pods}}}
        if "podTerminate" in query:
            # The mutation embeds the id it was asked to kill; record it and
            # drop that pod from the listing so the confirmation poll converges.
            for pod in list(self.pods):
                if str(pod["id"]) in query:
                    self.terminated.append(str(pod["id"]))
                    self.pods.remove(pod)
                    break
            else:
                self.terminated.append("<unmatched>")
            return {}
        if "pod(input:" in query:
            for pod in self.pods:
                if str(pod["id"]) in query:
                    return {"data": {"pod": {"id": pod["id"]}}}
            return {"data": {"pod": None}}
        return {}


def _pod(pod_id: str, name: str) -> dict[str, Any]:
    return {"id": pod_id, "desiredStatus": "RUNNING", "imageName": "img", "name": name}


def _provider(transport: _Transport) -> RunPodProvider:
    return RunPodProvider(http_post=transport, sleep=lambda _s: None)


class _UtilStub:
    """Stands in for ``RunPodGraphQLUtilEndpoint``: answers only real pod ids."""

    def __init__(self, *, known: dict[str, UtilSnapshot | None]) -> None:
        self.known = known
        self.asked: list[str] = []

    def probe(self, instance_id: str) -> tuple[bool, UtilSnapshot | None]:
        self.asked.append(instance_id)
        if instance_id not in self.known:
            return (False, None)
        return (True, self.known[instance_id])


def _snapshot() -> UtilSnapshot:
    return UtilSnapshot(
        gpu_util_percent=97.0,
        cpu_percent=41.0,
        memory_percent=63.0,
        disk_percent=12.0,
        uptime_seconds=600,
    )


# ---------------------------------------------------------------------------
# destroy_instance
# ---------------------------------------------------------------------------


def test_destroy_by_reserved_name_terminates_the_pod_runpod_actually_has() -> None:
    """``destroy_instance(<name>)`` must kill the pod that CARRIES that name.

    U16's headline. Pre-fix the terminate mutation carried the name itself,
    which RunPod's ``podTerminate`` does not accept as a ``podId``, so the pod
    survived — and the confirmation poll, asking about the same unknown string,
    got ``data.pod = null`` and read it as proof of death.
    """
    transport = _Transport(pods=[_pod(_REAL_ID, _RESERVED_NAME)])
    _provider(transport).destroy_instance(_RESERVED_NAME)
    assert transport.terminated == [_REAL_ID]


def test_destroying_by_name_is_not_reported_done_while_the_pod_is_listed() -> None:
    """The confirmation poll must follow the RESOLVED id, not the name.

    Catches a half-fix that resolves the name for the terminate but leaves the
    poll asking ``pod(input:{podId: <name>})`` — which returns null for a
    string RunPod does not know, so the destroy would report success on its
    first poll no matter what the terminate did.
    """
    transport = _Transport(pods=[_pod(_REAL_ID, _RESERVED_NAME)])
    provider = _provider(transport)

    # Terminate that does NOT remove the pod: the poll must keep seeing it and
    # ultimately refuse to claim success.
    def _stubborn(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        if "podTerminate" in str(payload.get("query", "")):
            return {}
        return transport(url, payload)

    provider._http_post = _stubborn
    with pytest.raises(TeardownError):
        provider.destroy_instance(_RESERVED_NAME)


def test_an_id_match_wins_over_a_namesake() -> None:
    """A real pod id is used as-is, even when another pod is NAMED that string.

    Catches a resolver that checks names before ids: ``destroy --id pod-7yq2m1``
    would then terminate whichever unrelated pod an operator had happened to
    name ``pod-7yq2m1`` — a destroy landing on the wrong resource, which is the
    worst outcome available to this code path.
    """
    transport = _Transport(
        pods=[
            _pod(_REAL_ID, "some-other-name"),
            _pod("pod-impostor", _REAL_ID),
        ]
    )
    _provider(transport).destroy_instance(_REAL_ID)
    assert transport.terminated == [_REAL_ID]


def test_an_ambiguous_name_is_refused_rather_than_guessed() -> None:
    """Two pods sharing the name → refuse, naming the ambiguity.

    RunPod does not enforce unique pod names. Picking "the first match" would
    destroy an arbitrary one of two live pods and report success. This is the
    same duplicate-name hazard U17 round 3 had to retrofit into the Modal
    destroy path after the fact; here it is refused up front.
    """
    transport = _Transport(
        pods=[_pod("pod-one", _RESERVED_NAME), _pod("pod-two", _RESERVED_NAME)]
    )
    with pytest.raises(TeardownError, match=_RESERVED_NAME):
        _provider(transport).destroy_instance(_RESERVED_NAME)
    assert transport.terminated == []


def test_destroy_of_an_already_gone_pod_stays_idempotent() -> None:
    """An identifier that resolves to nothing behaves exactly as it does today.

    Load-bearing, and the reason the name lookup does not raise on a miss: an
    already-destroyed pod is ALSO absent from the listing, and every
    ``--no-reuse`` teardown and every reaper act path ends with a destroy that
    must be safe to repeat. A resolver that raised "unknown identifier" would
    turn a clean exit into an error on the most common path there is.
    """
    transport = _Transport(pods=[])
    _provider(transport).destroy_instance("pod-long-gone")  # must not raise


def test_a_listing_failure_does_not_break_a_destroy_that_would_have_worked() -> None:
    """An unreadable listing degrades to the plain id path, it does not fail.

    U17's round-2 lesson applied before the fact: a lookup added for a narrow
    recovery case must never be able to fail the destroy that works today. A
    transient GraphQL fault on the LIST query would otherwise strand a pod the
    operator was actively tearing down.
    """
    transport = _Transport(pods=[], list_raises=RuntimeError("graphql 502"))
    _provider(transport).destroy_instance("pod-abc123")  # must not raise
    assert transport.terminated == ["<unmatched>"]


# ---------------------------------------------------------------------------
# probe_runtime
# ---------------------------------------------------------------------------


def test_probe_falls_back_to_the_name_when_the_id_probe_404s() -> None:
    """A 404 on the reserved name must be retried against the resolved pod id.

    Symptom 3 of the original U8 filing, surviving verbatim on RunPod. During
    the cold boot the ephemeral index row's id IS the name, so every sweeper
    tick read ``found=False`` on a live, billing pod — indistinguishable from a
    phantom, and GC_404 deletes the row that was the only handle on it.
    """
    transport = _Transport(pods=[_pod(_REAL_ID, _RESERVED_NAME)])
    provider = _provider(transport)
    provider._util_endpoint = _UtilStub(known={_REAL_ID: _snapshot()})  # type: ignore[assignment]

    probe = provider.probe_runtime(_RESERVED_NAME)

    assert probe is not None
    assert probe.found is True
    assert probe.gpu_util_pct == 97.0
    assert probe.cpu_pct == 41.0


def test_the_probe_reports_the_identifier_it_was_asked_about() -> None:
    """``RuntimeProbe.pod_id`` stays the caller's key, not the resolved id.

    The reaper keys its snapshot, its stall history and its act path by the
    index row's id. Returning the resolved id here would silently split those
    keys in two, so the verdict would be computed for one string and the
    destroy attempted against a row that no longer matches it.
    """
    transport = _Transport(pods=[_pod(_REAL_ID, _RESERVED_NAME)])
    provider = _provider(transport)
    provider._util_endpoint = _UtilStub(known={_REAL_ID: _snapshot()})  # type: ignore[assignment]

    probe = provider.probe_runtime(_RESERVED_NAME)

    assert probe is not None
    assert probe.pod_id == _RESERVED_NAME


def test_a_genuinely_absent_pod_still_reports_not_found() -> None:
    """Nothing matching in the listing → ``found=False``, exactly as today.

    Catches a fallback that fabricates liveness. GC_404 is the verdict that
    clears dead rows; if the probe can never say "not found", an ephemeral
    index row for a pod that really is gone haunts ``kinoforge list`` forever.
    """
    transport = _Transport(pods=[])
    provider = _provider(transport)
    provider._util_endpoint = _UtilStub(known={})  # type: ignore[assignment]

    probe = provider.probe_runtime("kinoforge-00000000")

    assert probe is not None
    assert probe.found is False


def test_a_listing_failure_during_the_fallback_leaves_the_404_intact() -> None:
    """An unreadable listing must not convert a 404 into a transport fault.

    The sweeper classifies a raising probe as PROBE_FAILED, a DIFFERENT verdict
    from GC_404 with different policy consequences. Letting the fallback's list
    call raise would relabel every genuine 404 the moment RunPod's API hiccups.
    """
    transport = _Transport(pods=[], list_raises=RuntimeError("graphql 502"))
    provider = _provider(transport)
    provider._util_endpoint = _UtilStub(known={})  # type: ignore[assignment]

    probe = provider.probe_runtime("kinoforge-00000000")

    assert probe is not None
    assert probe.found is False


def test_the_name_fallback_is_skipped_when_the_id_probe_succeeds() -> None:
    """No listing query at all on the common path.

    The sweeper probes every ephemeral row on every heartbeat. An unconditional
    list would add one GraphQL read per pod per tick forever, to serve a
    fallback that only matters during a cold boot.
    """
    transport = _Transport(pods=[_pod(_REAL_ID, _RESERVED_NAME)])
    provider = _provider(transport)
    provider._util_endpoint = _UtilStub(known={_REAL_ID: _snapshot()})  # type: ignore[assignment]

    provider.probe_runtime(_REAL_ID)

    assert transport.list_calls == 0


def test_a_booting_pod_is_resolvable_before_it_reaches_ready() -> None:
    """Resolution must not require ``status == "ready"``.

    The window U16 exists for IS the multi-minute cold boot, where the pod's
    ``desiredStatus`` has not settled. ``find_instance_by_tag`` — the obvious
    thing to reuse, since it already matches ``tags["name"]`` — filters its
    listing path to ready instances, so reusing it would have produced a
    resolver that silently misses every pod it was written to find, and the
    only symptom would be U16 continuing to look unfixed.
    """
    transport = _Transport(pods=[])
    transport.pods.append(
        {
            "id": _REAL_ID,
            "desiredStatus": "CREATED",
            "imageName": "img",
            "name": _RESERVED_NAME,
        }
    )
    _provider(transport).destroy_instance(_RESERVED_NAME)
    assert transport.terminated == [_REAL_ID]
