"""Unit tests for :mod:`tools.preflight`.

Preflight is the gate that must pass BEFORE any live-spend tool
(``capture_object_info.py``, ``tests/live/``) is run. It checks three
invariants:

1. Required creds are set (``RUNPOD_API_KEY``,
   ``RUNPOD_TERMINATE_KEY``, ``HF_TOKEN``). Auto-loaded from ``.env``
   by ``main()``; tests inject via ``env_getter``.
2. Git working tree is clean — uncommitted scaffolds would be lost if
   a live-spend tool crashes mid-run.
3. Zero active RunPod pods — any active pod is either still billing
   from a previous session or someone else's resource that preflight
   would otherwise destroy.

All four checks must run even if an earlier one fails; the operator
should see the full state, not just the first problem.
"""

from __future__ import annotations

import pytest


def _good_env() -> dict[str, str]:
    return {
        "RUNPOD_API_KEY": "rpa_DEADBEEFCAFEBABE12345678",
        "RUNPOD_TERMINATE_KEY": "rpa_TERMKEY1234567890ABCDEF",
        "HF_TOKEN": "hf_FAKEXXXXXXXXXXXXXXXXXXXXXXXX",
    }


def test_all_clean_returns_zero_with_every_check_marked_pass() -> None:
    """Bug it catches: a preflight that reports success even when one
    check silently errored. Every check line must explicitly carry an
    OK marker on the happy path so an operator can confirm coverage by
    reading the output.
    """
    from tools.preflight import run_preflight

    code, lines = run_preflight(
        env_getter=_good_env().get,
        pod_lister=lambda: [],
        git_dirty=lambda: "",
    )

    assert code == 0
    joined = "\n".join(lines)
    assert joined.count("OK") >= 3  # env, pods, git — one OK each minimum
    assert "FAIL" not in joined


def test_missing_required_env_fails_with_var_name_in_output() -> None:
    """Bug it catches: env-check that fails-fast on the FIRST missing
    var only. Operator should see every missing var in one report so
    they can fix all in one pass. Also catches a check that returns
    zero exit code despite reporting failures (most common preflight
    bug).
    """
    from tools.preflight import run_preflight

    env = _good_env()
    del env["RUNPOD_API_KEY"]
    del env["HF_TOKEN"]

    code, lines = run_preflight(
        env_getter=env.get,
        pod_lister=lambda: [],
        git_dirty=lambda: "",
    )

    assert code != 0
    joined = "\n".join(lines)
    assert "RUNPOD_API_KEY" in joined
    assert "HF_TOKEN" in joined


def test_active_pod_fails_with_pod_id_in_output() -> None:
    """Bug it catches: pod-scan that returns OK when a pod IS active
    because the lister returned a truthy list but the check inverted
    the predicate. Pod id must appear in the report so the operator
    knows which one to destroy.
    """
    from tools.preflight import run_preflight

    code, lines = run_preflight(
        env_getter=_good_env().get,
        pod_lister=lambda: [{"id": "abc123leaked", "name": "leaked"}],
        git_dirty=lambda: "",
    )

    assert code != 0
    joined = "\n".join(lines)
    assert "abc123leaked" in joined


def test_dirty_git_tree_fails_with_modified_paths_shown() -> None:
    """Bug it catches: dirty-tree check that ignores ``?? untracked``
    porcelain lines. Untracked scaffolds (e.g. a freshly-written
    ``tools/capture_object_info.py``) are EXACTLY what the durability
    rule cares about — losing them across a crash is the regression
    this rule prevents.
    """
    from tools.preflight import run_preflight

    porcelain = " M src/kinoforge/foo.py\n?? tools/capture_object_info.py\n"
    code, lines = run_preflight(
        env_getter=_good_env().get,
        pod_lister=lambda: [],
        git_dirty=lambda: porcelain,
    )

    assert code != 0
    joined = "\n".join(lines)
    assert "tools/capture_object_info.py" in joined


def test_all_failures_reported_even_when_first_check_fails() -> None:
    """Bug it catches: checks chained with early-return on first
    failure. Operator hitting a fresh shell needs to see ALL gaps in
    one pass, not fix-then-rerun-then-fix-then-rerun.
    """
    from tools.preflight import run_preflight

    env = _good_env()
    del env["HF_TOKEN"]

    code, lines = run_preflight(
        env_getter=env.get,
        pod_lister=lambda: [{"id": "pod_x", "name": "x"}],
        git_dirty=lambda: " M README.md\n",
    )

    assert code != 0
    joined = "\n".join(lines)
    assert "HF_TOKEN" in joined
    assert "pod_x" in joined
    assert "README.md" in joined


def test_pod_lister_default_redacts_env_field_from_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug it catches: a future maintainer wiring the default REST pod
    lister to dump full pod records (including ``env``) into the
    preflight checklist. Real RunPod ``GET /v1/pods`` returns the env
    block in plaintext; the report layer must never include it.

    The contract pinned here: ``run_preflight`` never prints anything
    from a pod record beyond ``id`` + ``name`` + ``costPerHr``, even
    when the lister returns a record carrying secrets.
    """
    from tools.preflight import run_preflight

    leaky_pod = {
        "id": "pod_y",
        "name": "y",
        "costPerHr": 0.69,
        "env": {"HF_TOKEN": "hf_REALLOOKINGTOKENVALUE12345"},
        "secret_url": "https://x?token=rpa_LEAKYTOKEN12345678",
    }

    _code, lines = run_preflight(
        env_getter=_good_env().get,
        pod_lister=lambda: [leaky_pod],
        git_dirty=lambda: "",
    )
    joined = "\n".join(lines)
    assert "hf_REALLOOKINGTOKENVALUE12345" not in joined
    assert "rpa_LEAKYTOKEN12345678" not in joined
    assert "pod_y" in joined  # id still surfaced


def test_check_no_active_sky_clusters_skipped_when_skypilot_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When skypilot is not installed, the check skips with a clear log line.

    Bug catch: a future refactor that turns the ImportError into a hard
    failure would break preflight on any developer machine without skypilot.
    """
    import builtins
    import sys
    from typing import Any

    sys.modules.pop("sky", None)
    real_import = builtins.__import__

    def _fail_sky(name: str, *a: Any, **kw: Any) -> Any:
        if name == "sky":
            raise ImportError("skypilot not installed")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fail_sky)
    from tools.preflight import _check_no_active_sky_clusters

    caplog.set_level("INFO")
    assert _check_no_active_sky_clusters() is True
    assert "skypilot not installed" in caplog.text


def test_check_no_active_sky_clusters_passes_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When sky.status() returns empty, the check passes.

    Bug catch: a future refactor that returns False on empty lists would
    block live spend even when the project is in the clean state required.
    """
    import sys
    import types

    fake_sky = types.ModuleType("sky")
    fake_sky.status = lambda **kw: []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sky", fake_sky)

    from tools.preflight import _check_no_active_sky_clusters

    assert _check_no_active_sky_clusters() is True


def test_check_no_active_sky_clusters_fails_when_cluster_up(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When sky.status() shows an UP or INIT cluster, the check fails loud.

    Bug catch: skipping leaked clusters silently would cause live-spend
    runs to fail intermittently when prior runs leaked state.
    """
    import sys
    import types

    fake_sky = types.ModuleType("sky")
    fake_sky.status = lambda **kw: [  # type: ignore[attr-defined]
        {"name": "leftover-cluster", "status": "UP"},
        {"name": "another", "status": "INIT"},
    ]
    monkeypatch.setitem(sys.modules, "sky", fake_sky)

    from tools.preflight import _check_no_active_sky_clusters

    assert _check_no_active_sky_clusters() is False
    err = capsys.readouterr().err
    assert "leftover-cluster" in err
    assert "another" in err


def test_check_no_active_sky_clusters_resolves_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Modern SkyPilot returns a ``RequestId`` from ``status()``; the check
    must resolve via ``sky.get(request_id)`` to get the list of clusters.

    Bug catch: a refactor that drops the ``isinstance(clusters, list)``
    branch would silently iterate the ``RequestId`` object (or raise
    ``TypeError``), breaking preflight against any modern SkyPilot SDK.
    """
    import sys
    import types

    fake_sky = types.ModuleType("sky")
    sentinel_request_id = object()  # opaque RequestId stand-in
    fake_sky.status = lambda **kw: sentinel_request_id  # type: ignore[attr-defined]
    fake_sky.get = lambda req: [  # type: ignore[attr-defined]
        {"name": "modern-cluster", "status": "ClusterStatus.UP"},
    ]
    monkeypatch.setitem(sys.modules, "sky", fake_sky)

    from tools.preflight import _check_no_active_sky_clusters

    # Should resolve the RequestId via sky.get(), see the cluster, and
    # collapse "ClusterStatus.UP" to "UP" via rsplit before checking
    # membership in {"UP", "INIT"}.
    assert _check_no_active_sky_clusters() is False
