"""The provider-side resource name is minted controller-side, once per launch.

Spec A2 fix round 1. ``--ephemeral`` binds STRICT_POLICY, whose
``pod_name_includes_alias=False`` means the alias-laden ``run_id`` must never
reach the provider-visible name. Until now each provider satisfied that by
minting its own random token *inside* ``create_instance``
(``providers/modal/__init__.py``, ``providers/runpod/__init__.py``), so the
name did not exist until the create was already in flight — and the pre-create
index row, whose entire purpose is to be a handle on a resource that is already
billing, could not name it.

The mint therefore moves onto :class:`EphemeralSession`: the controller asks
for the name before the create, the provider asks for the same name inside it,
and both get the same string. STRICT_POLICY's guarantee is unchanged — a random
token with no alias in it.
"""

from __future__ import annotations

import re

from kinoforge.core.ephemeral import EphemeralSession

_LEAKY_RUN_ID = "upscale-20260712-200409"


def test_resource_name_is_the_run_id_under_the_default_policy() -> None:
    """Bug caught: opaque naming bleeding into ordinary runs. The run id IS
    the resource name outside ``--ephemeral`` — every warm-attach ledger key,
    log line and teardown message names the app by it, and
    ``cli/_reconcile._adopt_launching_row`` matches a launching row against it.
    """
    with EphemeralSession(enabled=False) as session:
        assert session.resource_name(_LEAKY_RUN_ID, "modal") == _LEAKY_RUN_ID
        assert session.resource_name(_LEAKY_RUN_ID, "runpod") == _LEAKY_RUN_ID


def test_resource_name_is_opaque_and_alias_free_under_strict() -> None:
    """Bug caught: leaking the subcommand + local timestamp into the
    provider-visible name. On Modal a stopped app lingers in ``modal app list``
    forever, so ``upscale-20260712-200409`` is a permanent public record of
    when the operator ran what.
    """
    with EphemeralSession(enabled=True) as session:
        modal_name = session.resource_name(_LEAKY_RUN_ID, "modal")
        runpod_name = session.resource_name(_LEAKY_RUN_ID, "runpod")

    assert re.fullmatch(r"eph-[0-9a-f]{8}", modal_name), modal_name
    assert re.fullmatch(r"kinoforge-[0-9a-f]{8}", runpod_name), runpod_name
    for name in (modal_name, runpod_name):
        assert "upscale" not in name
        assert "2026" not in name


def test_resource_name_is_stable_for_one_run_id() -> None:
    """The controller and the provider must land on the SAME string.

    Bug caught: minting a fresh token per call — which is what each provider
    did internally. The pre-create row would then carry a name the provider
    never used, so ``kinoforge destroy --id`` misses it, it is absent from the
    provider console, and ``reaper_actor``'s probe reports "not found" in a way
    the classifier cannot tell from a dead phantom.
    """
    with EphemeralSession(enabled=True) as session:
        first = session.resource_name(_LEAKY_RUN_ID, "modal")
        second = session.resource_name(_LEAKY_RUN_ID, "modal")
        # The prefix is provider-shaped; the TOKEN is a property of the launch,
        # so asking on behalf of another provider must not re-roll it.
        runpod = session.resource_name(_LEAKY_RUN_ID, "runpod")

    assert first == second
    assert runpod == f"kinoforge-{first.removeprefix('eph-')}"


def test_resource_name_differs_between_launches() -> None:
    """Bug caught: memoising per SESSION rather than per launch. ``--ephemeral
    batch`` drives several cold creates inside one session; a shared token
    would give two Modal apps the same id, and the second create would collide
    with — or silently replace — the first.
    """
    with EphemeralSession(enabled=True) as session:
        a = session.resource_name("run-a", "modal")
        b = session.resource_name("run-b", "modal")

    assert a != b


def test_resource_name_is_never_memoised_for_an_empty_run_id() -> None:
    """Bug caught: keying the memo on an empty run id, which would hand every
    anonymous launch in a session the same provider-side name. A name that is
    not unique is not a handle.
    """
    with EphemeralSession(enabled=True) as session:
        first = session.resource_name("", "runpod")
        second = session.resource_name("", "runpod")

    assert re.fullmatch(r"kinoforge-[0-9a-f]{8}", first), first
    assert first != second


def test_an_unknown_provider_still_gets_an_opaque_name() -> None:
    """Bug caught: a KeyError on a provider the prefix table does not list,
    which would turn adding a provider into a crash on the ephemeral path
    rather than a naming-cosmetics decision.
    """
    with EphemeralSession(enabled=True) as session:
        name = session.resource_name("run-a", "some-new-cloud")

    assert re.fullmatch(r"kinoforge-[0-9a-f]{8}", name), name


# ---------------------------------------------------------------------------
# Destroy confirmation — the other half of the row's usability.
# ---------------------------------------------------------------------------


def test_destroy_is_confirmed_until_something_says_otherwise() -> None:
    """Bug caught: defaulting to "unconfirmed", which would make every clean
    ``--no-reuse`` run keep a launch row for a pod that is definitely gone.
    """
    with EphemeralSession(enabled=True) as session:
        assert session.destroy_was_confirmed("pod-1") is True


def test_marking_a_destroy_unconfirmed_is_visible_to_the_caller() -> None:
    """Bug caught: the orchestrator swallowing ``TeardownError`` with no
    durable trace, so the CLI deletes the last record of a pod that may still
    be billing. The mark is the only channel between the two.
    """
    with EphemeralSession(enabled=True) as session:
        session.mark_destroy_unconfirmed("pod-1")
        assert session.destroy_was_confirmed("pod-1") is False
        assert session.destroy_was_confirmed("pod-2") is True
