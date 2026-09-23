"""Behavior: the shared on-pod directory layout, derived from a volume mount.

This helper exists because ``/workspace`` is RunPod's volume mount and Modal
mounts at ``/cache/hf``. A server module that hardcodes either one is wrong on
the other provider and wrong on every CI runner, which is the defect this whole
plan removes. The layout is named once, here, and the providers supply the mount.
"""

from __future__ import annotations

import inspect

from kinoforge.core import pod_paths
from kinoforge.core.pod_paths import pod_path_env


def test_dirs_are_rooted_on_the_supplied_mount() -> None:
    """A mount produces artifact + loras dirs beneath it.

    Catches a helper that ignores its argument and returns a constant — which
    would reintroduce exactly the hardcoded-path defect it exists to remove.
    """
    env = pod_path_env("/mnt/example-volume")

    assert env["KINOFORGE_ARTIFACT_DIR"] == "/mnt/example-volume/artifacts"
    assert env["KINOFORGE_LORAS_DIR"] == "/mnt/example-volume/loras"


def test_hf_home_is_absent_unless_the_caller_supplies_one() -> None:
    """The helper never invents an HF cache location.

    Catches deriving HF_HOME from the mount. Modal's HF_HOME is the Volume ROOT
    and that Volume holds a 144 GiB fetch; a derived ``<mount>/.hf_cache`` would
    orphan it and silently re-download 123.8 GiB. The difference between Modal's
    root and RunPod's subdir is real provider policy, so it is passed in.
    """
    assert "HF_HOME" not in pod_path_env("/mnt/example-volume")


def test_supplied_hf_home_is_passed_through_verbatim() -> None:
    """Whatever the provider names is what lands in the env.

    Catches a helper that accepts the argument then rewrites it — the
    pass-through is the entire point of making it an argument.
    """
    env = pod_path_env("/mnt/example-volume", hf_home="/mnt/example-volume")

    assert env["HF_HOME"] == "/mnt/example-volume"


def test_no_volume_falls_back_to_pod_local_scratch() -> None:
    """A volumeless host (SkyPilot, Local) still gets writable dirs.

    Catches emitting a mount-rooted path built from an empty string, which
    yields bare ``/artifacts`` — unwritable on every host, and the same class
    of failure as the /workspace bug.
    """
    for empty in ("", None):
        env = pod_path_env(empty)

        assert env["KINOFORGE_ARTIFACT_DIR"] == pod_paths.SCRATCH_ARTIFACT_DIR
        assert env["KINOFORGE_LORAS_DIR"] == pod_paths.SCRATCH_LORAS_DIR
        assert env["KINOFORGE_ARTIFACT_DIR"].startswith("/tmp/")
        assert "HF_HOME" not in env, (
            "a volumeless host must inherit huggingface_hub's own default"
        )


def test_trailing_slash_does_not_double_the_separator() -> None:
    """``/mnt/vol/`` and ``/mnt/vol`` produce the same paths.

    Catches naive f-string concatenation. A doubled slash is survivable on
    POSIX but makes the golden launch payloads differ for configs that are
    semantically identical, which corrupts the ratchet.
    """
    assert pod_path_env("/mnt/vol/") == pod_path_env("/mnt/vol")


def test_helper_is_pure_and_provider_agnostic() -> None:
    """No provider import, and no provider mount inside the logic.

    Catches the helper growing a dependency on the providers it serves, which
    would make it uncallable from inside one without a circular import.

    The mount check is scoped to the FUNCTION, not the module, and that is
    deliberate: the module docstring names both ``/workspace`` and
    ``/cache/hf`` to explain why this helper exists, and a module-wide
    substring check cannot tell an explanation from a runtime default. The
    function's own source is where a hardcoded mount would actually do harm.
    Task 5's audit does not cover this file — it scans ``os.environ``
    fallbacks, and there are none here — so this assertion is the only guard
    on it.
    """
    module_source = inspect.getsource(pod_paths)
    function_source = inspect.getsource(pod_path_env)

    assert "kinoforge.providers" not in module_source
    assert "/workspace" not in function_source, "RunPod's mount must not appear here"
    assert "/cache/hf" not in function_source, "Modal's mount must not appear here"
