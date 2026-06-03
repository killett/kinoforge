"""Unit tests for :mod:`tools.preflight`.

Preflight is the gate that must pass BEFORE any live-spend tool
(``capture_object_info.py``, ``tests/live/``) is run. It checks four
invariants:

1. Required env vars are set (``KINOFORGE_LIVE_TESTS=1``,
   ``RUNPOD_API_KEY``, ``RUNPOD_TERMINATE_KEY``, ``HF_TOKEN``).
2. Git working tree is clean — uncommitted scaffolds would be lost if
   a live-spend tool crashes mid-run.
3. Zero active RunPod pods — any active pod is either still billing
   from a previous session or someone else's resource that preflight
   would otherwise destroy.
4. Required tooling on PATH — currently a no-op slot; reserved.

All four checks must run even if an earlier one fails; the operator
should see the full state, not just the first problem.
"""

from __future__ import annotations

import pytest


def _good_env() -> dict[str, str]:
    return {
        "KINOFORGE_LIVE_TESTS": "1",
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
    del env["KINOFORGE_LIVE_TESTS"]

    code, lines = run_preflight(
        env_getter=env.get,
        pod_lister=lambda: [{"id": "pod_x", "name": "x"}],
        git_dirty=lambda: " M README.md\n",
    )

    assert code != 0
    joined = "\n".join(lines)
    assert "KINOFORGE_LIVE_TESTS" in joined
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
