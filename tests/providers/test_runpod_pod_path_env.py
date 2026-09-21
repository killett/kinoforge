"""Behavior: RunPod tells the pod where its writable directories are.

Before this seam, the server guessed — it hardcoded ``/workspace`` as a module
constant read at import. That is RunPod's volume mount, so it was right here by
luck and wrong on Modal and on every CI runner. The provider knows its own
mount; it is the only component that can answer this.
"""

from __future__ import annotations

from kinoforge.core.interfaces import InstanceSpec
from kinoforge.providers.runpod import RunPodProvider


def _env_for(spec: InstanceSpec) -> dict[str, str]:
    """Assemble the create-pod env for *spec*.

    Args:
        spec: The instance specification under test.

    Returns:
        The env dict the create mutation would carry.
    """
    provider = RunPodProvider()
    return provider._assemble_create_env(spec)


def test_exports_the_pod_dirs_rooted_on_the_default_mount() -> None:
    """A spec with no explicit mount gets RunPod's own ``/workspace``.

    Catches the provider staying silent and leaving the server to guess, which
    is the whole defect: a silent provider means the server needs a default,
    and any default it picks is another provider's path somewhere.
    """
    env = _env_for(InstanceSpec(image="python:3.13-slim"))

    assert env["KINOFORGE_ARTIFACT_DIR"] == "/workspace/artifacts"
    assert env["KINOFORGE_LORAS_DIR"] == "/workspace/loras"


def test_exports_hf_home_as_a_subdir_of_the_volume() -> None:
    """``HF_HOME`` lands on the volume, not on container disk.

    This is the load-bearing one. Until this plan, the pinning came from the
    server's own import-time setdefault, which Task 4 deletes. If the provider
    does not carry it, a 70 GB Wan shard download goes to the 250 GB container
    disk instead of the volume — the precise failure the comment at
    wan_t2v_server.py:47-49 was written to prevent.
    """
    env = _env_for(InstanceSpec(image="python:3.13-slim"))

    assert env["HF_HOME"] == "/workspace/.hf_cache"


def test_an_explicit_mount_moves_all_three() -> None:
    """A spec-supplied mount is honoured, not ignored.

    Catches hardcoding ``/workspace`` a second time inside the new code rather
    than reading the mount the spec declares.
    """
    env = _env_for(
        InstanceSpec(image="python:3.13-slim", volume_mount="/mnt/example-volume")
    )

    assert env["KINOFORGE_ARTIFACT_DIR"] == "/mnt/example-volume/artifacts"
    assert env["KINOFORGE_LORAS_DIR"] == "/mnt/example-volume/loras"
    assert env["HF_HOME"] == "/mnt/example-volume/.hf_cache"


def test_config_supplied_values_win_over_the_derived_ones() -> None:
    """An explicit env value is not clobbered by the derivation.

    Catches using plain assignment instead of ``setdefault``. A cfg that
    deliberately redirects its artifact dir must keep doing so, or this seam
    becomes a regression for anyone already overriding it.
    """
    env = _env_for(
        InstanceSpec(
            image="python:3.13-slim",
            env={
                "KINOFORGE_ARTIFACT_DIR": "/mnt/somewhere-else",
                "HF_HOME": "/mnt/hf",
            },
        )
    )

    assert env["KINOFORGE_ARTIFACT_DIR"] == "/mnt/somewhere-else"
    assert env["HF_HOME"] == "/mnt/hf"
    assert env["KINOFORGE_LORAS_DIR"] == "/workspace/loras", (
        "the keys the caller did NOT set must still be derived"
    )
