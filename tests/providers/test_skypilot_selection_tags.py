"""Behavior: what SkyPilot actually booked is on the Instance.

S4 shipped a cap violation whose message read ``provider=skypilot`` and
nothing else, because a SkyPilotProvider Instance carried no selection tags
and _placement_summary reports only what an Instance really holds. An operator
reading that cannot tell "my cap is too low" from "this went somewhere I did
not intend", which is the whole stated purpose of the summary.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import RateCapExceeded
from kinoforge.core.interfaces import InstanceSpec, Lifecycle, Placement
from kinoforge.core.orchestrator import _enforce_rate_cap
from kinoforge.providers.skypilot import SkyPilotProvider, _format_accelerators


class _Resources:
    """Stand-in for ``sky.Resources`` — exposes exactly what sky's real
    ``Resources`` object exposes for a booked cluster: ``instance_type``,
    ``cloud``, ``region`` and ``accelerators``, plus ``get_cost`` (the same
    method ``realized_rate`` already reads).
    """

    def __init__(
        self,
        *,
        instance_type: str = "c6i.large",
        cloud: str = "AWS",
        region: str = "us-west-2",
        accelerators: Any = "",
        cost_per_hr: float = 0.085,
    ) -> None:
        self.instance_type = instance_type
        self.cloud = cloud
        self.region = region
        self.accelerators = accelerators
        self._cost_per_hr = cost_per_hr

    def get_cost(self, seconds: float) -> float:
        return self._cost_per_hr * (seconds / 3600.0)


class _Handle:
    """Stand-in for sky's cluster handle — the only field readers use."""

    def __init__(self, launched_resources: Any) -> None:
        self.launched_resources = launched_resources


class _ResourcesMissingRegion:
    """A launched-resources stand-in that does not expose ``region`` at all.

    Distinct from ``_Resources`` (which always sets all four attributes, even
    to falsy values) — this class has no ``region`` attribute whatsoever, so
    ``getattr(obj, "region", _MISSING)`` hits the sentinel branch. Proves the
    per-field omission path the brief's worked example describes ("minus any
    key the handle did not expose"), which is otherwise untested: the
    all-attributes-present fake never exercises ``_MISSING``, and the
    unreadable-handle fake (``handle=None``) short-circuits before the
    per-field loop runs at all.
    """

    def __init__(self) -> None:
        self.instance_type = "g5.xlarge"
        self.cloud = "AWS"
        self.accelerators = "A10G:1"
        # Deliberately no `region` attribute.


class _FakeSky:
    """Minimal injectable sky client: ``status``, ``launch``, ``down`` and the
    accelerator catalog. ``_make_spec`` below sets no ``launch``, so no ssh
    tunnel is opened and no other sky surface is touched.

    The catalog is NOT optional scenery. ``create_instance`` reads it whenever
    ``placement.max_usd_per_hr > 0`` — which is every spec here, since
    Placement defaults to $2.20 — and an ``AttributeError`` there is swallowed
    into "estimate unreadable". Without it every test in this file runs the
    degraded pre-launch branch, including
    ``test_the_rate_cap_message_names_the_sku``, which would then reach its
    post-launch readback only because the pre-launch arm is dead.
    """

    def __init__(self, status_result: list[dict[str, Any]]) -> None:
        self._status_result = status_result
        self.down_calls: list[str] = []
        self.catalog_calls: list[dict[str, Any]] = []

        class _TaskNamespace:
            @staticmethod
            def from_yaml_config(config: dict[str, Any]) -> Any:
                class _Task:
                    pass

                task = _Task()
                task.config = config  # type: ignore[attr-defined]
                return task

        self.Task = _TaskNamespace()

    def status(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return self._status_result

    def launch(self, task: Any, **kwargs: Any) -> Any:
        return (None, None)

    def list_accelerators(self, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        """Price A100 at $1.20 — under the $2.20 Placement default cap.

        Under the PRE-launch cap on purpose: this file's subject is the
        POST-launch readback (``_enforce_rate_cap`` against a $5.00 booking),
        and a pre-launch refusal would preempt it. What must not happen is the
        pre-launch arm being dead.
        """
        self.catalog_calls.append(dict(kwargs))
        return {
            "A100": [
                {
                    "accelerator_name": "A100",
                    "vram_gb": 80,
                    "cuda": "12.8",
                    "price": 1.20,
                }
            ]
        }

    def down(self, cluster_id: str) -> None:
        self.down_calls.append(cluster_id)


def _make_spec(**overrides: Any) -> InstanceSpec:
    """Minimal InstanceSpec naming an accelerator (S4 made selection
    provider-side, so a spec naming none would route to the catalog)."""
    base: dict[str, Any] = {
        "image": "pytorch/pytorch:2.3-cuda12.1-cudnn9-devel",
        "run_id": "kf-selection-probe",
        "lifecycle": Lifecycle(idle_timeout_s=7200.0),
        "placement": Placement(accelerators=("A100",)),
    }
    base.update(overrides)
    return InstanceSpec(**base)


def test_launch_records_the_chosen_sku_cloud_and_region() -> None:
    """The optimizer's choice lands on the Instance.

    Bug caught: a cap violation, a ledger row and a status line that all
    describe a cluster without saying what it is.
    """
    resources = _Resources(
        instance_type="c6i.large",
        cloud="AWS",
        region="us-west-2",
        accelerators="",
    )
    fake = _FakeSky(
        status_result=[
            {
                "name": "kf-selection-probe",
                "handle": _Handle(resources),
                "status": "UP",
            }
        ]
    )
    provider = SkyPilotProvider(sky_client=fake, sleep=lambda _s: None)

    instance = provider.create_instance(_make_spec())

    assert instance.tags["sku"] == "c6i.large"
    assert instance.tags["cloud"] == "AWS"
    assert instance.tags["region"] == "us-west-2"
    assert instance.tags["accelerators"] == ""
    # ``_make_spec`` declares no ``launch``, so no tunnel was opened and the
    # ``ports`` tag is correctly ABSENT — ``_ports_for`` reads it to decide
    # what ``ensure_endpoints`` should re-forward, and a server-less cluster
    # has nothing listening. The selection tags are an ADDITION to whatever the
    # merge produced, not a replacement, which is what this line pins.
    assert "ports" not in instance.tags


def test_an_unreadable_handle_writes_no_empty_tags() -> None:
    """Silence beats ``sku=``.

    Bug caught: writing empty strings makes _placement_summary emit
    "sku=, cloud=, provider=skypilot", which reads as a broken tool rather
    than as missing information.

    Scope note: ``handle=None`` short-circuits at ``_selection_tags``'s
    ``if launched is None: return {}`` before the per-field loop ever runs.
    This test therefore guards ONLY that whole-handle short-circuit, not the
    per-field ``_MISSING`` sentinel path — see
    ``test_a_field_the_handle_does_not_expose_is_omitted_while_the_rest_land``
    for that.
    """
    fake = _FakeSky(
        status_result=[{"name": "kf-selection-probe", "handle": None, "status": "UP"}]
    )
    provider = SkyPilotProvider(sky_client=fake, sleep=lambda _s: None)

    instance = provider.create_instance(_make_spec())

    for key in ("sku", "cloud", "region", "accelerators"):
        assert key not in instance.tags, (
            f"an unreadable handle must OMIT {key!r}, not write it as an empty string"
        )
    # The rest of the merge must still be intact. ``ports`` is absent because
    # this spec declares no ``launch`` (see the sibling test above), so what is
    # asserted is that the unreadable handle did not corrupt the tag map.
    assert "ports" not in instance.tags
    assert instance.tags == {}


def test_the_rate_cap_message_names_the_sku() -> None:
    """The S4 follow-up, end to end.

    Bug caught: the enrichment landing on the Instance but never reaching the
    error, e.g. because the tags are applied after the orchestrator's copy.
    """
    resources = _Resources(
        instance_type="c6i.large",
        cloud="AWS",
        region="us-west-2",
        accelerators="",
        # Above whatever cap the test sets below, so realized > cap.
        cost_per_hr=5.00,
    )
    fake = _FakeSky(
        status_result=[
            {
                "name": "kf-selection-probe",
                "handle": _Handle(resources),
                "status": "UP",
            }
        ]
    )
    provider = SkyPilotProvider(sky_client=fake, sleep=lambda _s: None)
    instance = provider.create_instance(_make_spec())

    # The pre-launch arm RAN and let this through on a $1.20 catalog floor
    # under the $2.20 default cap. Without this the test would still pass with
    # the estimate permanently unreadable, i.e. reaching the post-launch
    # readback only because the cheaper guard is dead.
    assert fake.catalog_calls, "the pre-launch cost estimate never read a catalog"

    with pytest.raises(RateCapExceeded) as exc_info:
        _enforce_rate_cap(provider=provider, instance=instance, cap=1.09)

    message = str(exc_info.value)
    assert "sku=c6i.large" in message
    assert "cloud=AWS" in message
    # The teardown this violation triggers must also have actually run.
    assert fake.down_calls == ["kf-selection-probe"]


def test_a_field_the_handle_does_not_expose_is_omitted_while_the_rest_land() -> None:
    """The ``_MISSING`` sentinel's actual job: per-field, not per-handle.

    Bug caught: collapsing "attribute absent" to the same ``""`` that
    "attribute present but falsy" produces (e.g. via ``getattr(obj, name,
    "")``) would make a genuinely unexposed ``region`` indistinguishable from
    a CPU booking's genuinely empty ``accelerators`` — the exact distinction
    the sentinel exists to preserve. Neither of the other two tests exercises
    this: the "records the chosen sku" test's fake exposes all four fields,
    and the "unreadable handle" test's ``handle=None`` short-circuits before
    the per-field loop runs.
    """
    fake = _FakeSky(
        status_result=[
            {
                "name": "kf-selection-probe",
                "handle": _Handle(_ResourcesMissingRegion()),
                "status": "UP",
            }
        ]
    )
    provider = SkyPilotProvider(sky_client=fake, sleep=lambda _s: None)

    instance = provider.create_instance(_make_spec())

    assert instance.tags["sku"] == "g5.xlarge"
    assert instance.tags["cloud"] == "AWS"
    assert instance.tags["accelerators"] == "A10G:1"
    assert "region" not in instance.tags, (
        "an attribute the handle never exposed must be OMITTED, not written "
        "as an empty string — writing it would make it indistinguishable "
        "from a genuinely-empty-but-exposed field"
    )


def test_a_stale_spec_tag_is_overwritten_by_a_genuine_empty_reading() -> None:
    """A truthful empty reading beats a stale caller guess of the same name.

    Bug caught (review finding IMPORTANT 1): nothing reserves the four
    selection-tag names — ``_strip_reserved_tags`` only reserves
    ``LAUNCH_PHASE_TAG``, and ``spec.tags`` is user-settable via a config's
    ``compute.tags``. A caller who set ``accelerators: some-note`` before a
    real CPU booking must see the truthful ``""`` reading, not their stale
    note — a merge that protects non-empty caller tags from an empty-but-read
    selection value would silently keep exactly the wrong information on the
    Instance the cap-violation summary reads from.
    """
    resources = _Resources(
        instance_type="c6i.large",
        cloud="AWS",
        region="us-west-2",
        accelerators="",  # genuinely read: a real CPU booking has none
    )
    fake = _FakeSky(
        status_result=[
            {
                "name": "kf-selection-probe",
                "handle": _Handle(resources),
                "status": "UP",
            }
        ]
    )
    provider = SkyPilotProvider(sky_client=fake, sleep=lambda _s: None)

    instance = provider.create_instance(
        _make_spec(tags={"accelerators": "stale-note-from-a-prior-run"})
    )

    assert instance.tags["accelerators"] == "", (
        "a genuinely-read empty selection value must overwrite a stale "
        "caller tag of the same name, not be shadowed by it"
    )


def test_a_stale_spec_tag_survives_an_unreadable_handle() -> None:
    """The flip side: nothing to merge means nothing changes.

    Bug caught (review finding IMPORTANT 1, converse case): when
    ``_selection_tags`` returns ``{}`` (handle unreadable), ``tags.update({})``
    is a no-op — a caller's own tag under one of the four selection-tag names
    must survive untouched. This is what distinguishes "unconditional
    overwrite" (correct) from "always clear these four keys first" (would
    also have passed the IMPORTANT-1 fix but destroyed caller data on every
    unreadable-handle launch).
    """
    fake = _FakeSky(
        status_result=[{"name": "kf-selection-probe", "handle": None, "status": "UP"}]
    )
    provider = SkyPilotProvider(sky_client=fake, sleep=lambda _s: None)

    instance = provider.create_instance(
        _make_spec(tags={"accelerators": "stale-note-from-a-prior-run"})
    )

    assert instance.tags["accelerators"] == "stale-note-from-a-prior-run"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"A100": 1}, "A100:1"),
        ({"A100": 1, "V100": 2}, "A100:1,V100:2"),
        ("A100:1", "A100:1"),
        (None, ""),
        ({}, ""),
        ("", ""),
    ],
)
def test_format_accelerators_covers_dict_str_none_and_empty(
    value: Any, expected: str
) -> None:
    """The one reason ``_format_accelerators`` exists: real sky's dict shape.

    Bug caught (review finding IMPORTANT 3): every other test in this file
    passes a plain string for ``accelerators``, so the dict branch — the
    entire reason the helper was added over a bare ``str(value)`` — had zero
    coverage. Without it, a real multi-GPU booking would land Python's dict
    repr (``"{'A100': 1, 'V100': 2}"``) in a cap-violation message instead of
    ``"A100:1,V100:2"``.
    """
    assert _format_accelerators(value) == expected
