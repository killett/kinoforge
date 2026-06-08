"""Pre-live-spend gate: env + git + active-pod scan.

Run before any tool that costs money (``capture_object_info.py``,
``tests/live/``). Reports every gap in one pass — failures DO NOT
short-circuit. Operator sees full state, fixes everything, re-runs.

Usage::

    pixi run preflight

Exit code 0 == all four checks passed. Non-zero == at least one
check failed; checklist on stdout names every gap.

Seam shape lets the unit tests inject fakes for env / pod-lister /
git-dirty without touching real RunPod or the test repo's git state.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

_HOSTED_REQUIRED_ENV = (
    "REPLICATE_API_TOKEN",
    "RUNWAYML_API_SECRET",
    "LUMAAI_API_KEY",
    "FAL_KEY",
)

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_log = logging.getLogger(__name__)


_REQUIRED_ENV = (
    "RUNPOD_API_KEY",
    "RUNPOD_TERMINATE_KEY",
    "HF_TOKEN",
)


def _list_pods_runpod() -> list[dict[str, Any]]:
    """Default pod-lister: hit RunPod REST ``GET /v1/pods`` with bearer.

    Returns:
        A list of pod records (``[]`` when none active). Raises
        :class:`urllib.error.HTTPError` on auth or transport failure;
        the caller surfaces that as a preflight FAIL line.
    """
    key = os.environ.get("RUNPOD_API_KEY")
    if not key:
        return []
    req = urllib.request.Request(
        "https://rest.runpod.io/v1/pods",
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "kinoforge-preflight/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        body = resp.read().decode()
    parsed: Any = json.loads(body)
    if isinstance(parsed, list):
        return [p for p in parsed if isinstance(p, dict)]
    return []


def _git_dirty_default() -> str:
    """Default git-dirty probe: ``git status --porcelain`` stdout.

    Returns:
        The porcelain output verbatim. Empty string == clean tree.
    """
    proc = subprocess.run(  # noqa: S603
        ["git", "status", "--porcelain"],  # noqa: S607
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout


def _record_field(record: Any, field: str) -> str:  # noqa: ANN401
    """Read ``field`` off a SkyPilot cluster record.

    Modern ``sky.status()`` returns typed pydantic-model records
    (``StatusResponse``, accessed via attributes); test fakes and legacy
    versions return plain dicts (accessed via ``.get()``). This helper
    handles both so the preflight scanner can stay shape-agnostic.

    Args:
        record: A cluster record from ``sky.status()`` — either a dict
            (test fake / legacy) or a ``StatusResponse`` (modern SDK).
        field: The field name to read (e.g. ``"name"``, ``"status"``).

    Returns:
        The field value coerced to ``str``; empty string if absent.
    """
    if isinstance(record, dict):
        value = record.get(field, "")
    else:
        value = getattr(record, field, "")
    return str(value)


def _check_no_active_sky_clusters() -> bool:
    """Verify no SkyPilot clusters are currently ``UP`` or ``INIT``.

    SkyPilot is optional infrastructure — this check skips silently if
    the SDK is not installed in the active env (e.g. the default env).
    When the SDK IS installed, any active cluster is treated as leaked
    state from a prior live run and fails preflight loud.

    Returns:
        ``True`` if safe to proceed (no active clusters OR skypilot not
        installed); ``False`` if leaked clusters were found. The
        offending cluster names are written to ``sys.stderr`` so the
        operator can ``sky down <name>`` each one before retrying.
    """
    try:
        import sky  # type: ignore[import-not-found, unused-ignore]
    except ImportError:
        _log.info("skypilot not installed; skipping SkyPilot cluster check")
        return True

    # Modern SkyPilot returns a RequestId from ``status()``; the actual
    # list of cluster records comes from ``sky.get(request_id)``. Older
    # versions and our unit-test fakes return the list directly, so we
    # only resolve via ``sky.get`` when ``status()`` did not give us a
    # list. The ``status`` field on each record is either a plain
    # string (``"UP"``) from older versions and test fakes, or a
    # ``ClusterStatus`` enum (str-able as ``"ClusterStatus.UP"``) from
    # modern SkyPilot. ``rsplit(".", 1)[-1]`` collapses both shapes to
    # the bare name. Field access is via :func:`_record_field`, which
    # accepts both dicts (fakes) and typed records (real SDK).
    clusters = sky.status()
    resolved_clusters: list[Any]
    if not isinstance(clusters, list):
        resolved_clusters = sky.get(clusters)
    else:
        resolved_clusters = clusters
    active = [
        c
        for c in resolved_clusters
        if _record_field(c, "status").rsplit(".", 1)[-1] in {"UP", "INIT"}
    ]
    if active:
        names = ", ".join(_record_field(c, "name") or "<unknown>" for c in active)
        print(
            f"FAIL: active SkyPilot clusters present: {names}\n"
            f"      run `sky down <name>` for each before invoking the live smoke",
            file=sys.stderr,
        )
        return False
    return True


def run_preflight(
    *,
    env_getter: Callable[[str], str | None],
    pod_lister: Callable[[], list[dict[str, Any]]],
    git_dirty: Callable[[], str],
) -> tuple[int, list[str]]:
    """Run all four preflight checks; return (exit_code, checklist_lines).

    Args:
        env_getter: Lookup function for environment variables (signature
            mirrors :meth:`os.environ.get`).
        pod_lister: Returns the list of currently-active RunPod pod
            records. Empty list == no leaks.
        git_dirty: Returns the ``git status --porcelain`` output. Empty
            string == clean working tree.

    Returns:
        Tuple of ``(exit_code, lines)``. ``exit_code`` is ``0`` only
        when every check passes; ``lines`` is the human-readable
        checklist (each line carries ``OK`` or ``FAIL``).

    Notes:
        Pod records are NEVER printed in full — only the ``id``,
        ``name``, and ``costPerHr`` fields. RunPod's REST response
        includes the pod's full ``env`` (with plaintext ``HF_TOKEN``
        etc.) and would leak if surfaced raw.
    """
    failed = False
    lines: list[str] = []

    # 1. Env vars
    missing = [k for k in _REQUIRED_ENV if not env_getter(k)]
    if missing:
        failed = True
        lines.append(f"  FAIL env: missing {missing}")
    else:
        lines.append(f"  OK   env: {list(_REQUIRED_ENV)} all set")

    # 2. RunPod pods
    try:
        pods = pod_lister()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        failed = True
        lines.append(f"  FAIL pods: lister error: {exc}")
        pods = []
    if pods:
        failed = True
        for p in pods:
            pid = p.get("id", "?")
            pname = p.get("name", "?")
            cost = p.get("costPerHr", "?")
            lines.append(f"  FAIL pods: active {pid} ({pname}) ${cost}/hr — destroy")
    else:
        lines.append("  OK   pods: 0 active")

    # 3. Git working tree
    porcelain = git_dirty()
    if porcelain.strip():
        failed = True
        lines.append("  FAIL git: working tree dirty")
        for raw in porcelain.splitlines():
            if raw:
                lines.append(f"       {raw}")
    else:
        lines.append("  OK   git: working tree clean")

    return (1 if failed else 0, lines)


def _check_hosted_credentials(env_getter: Callable[[str], str | None]) -> list[str]:
    """Return the list of missing Bearer-key env vars (empty when all present).

    Args:
        env_getter: Lookup function for environment variables.

    Returns:
        The names of the env vars in ``_HOSTED_REQUIRED_ENV`` that are
        missing or empty. Empty list == all four creds are present.
    """
    return [v for v in _HOSTED_REQUIRED_ENV if not env_getter(v)]


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: load ``.env``, run checks against real defaults, print, exit.

    ``.env`` load is silent if the file is absent. Returns the exit
    code from :func:`run_preflight` (``0`` on full pass).

    Args:
        argv: Optional argument list (default: ``sys.argv[1:]``). Tests
            pass this directly to drive ``--check-hosted`` without
            patching ``sys.argv``.
    """
    parser = argparse.ArgumentParser(
        prog="preflight", description="Pre-live-spend gate"
    )
    parser.add_argument(
        "--check-hosted",
        action="store_true",
        help=(
            "Verify hosted Bearer credentials are present in env: "
            "REPLICATE_API_TOKEN, RUNWAYML_API_SECRET, LUMAAI_API_KEY, FAL_KEY. "
            "Exits 2 with each missing var named on stderr."
        ),
    )
    args = parser.parse_args(argv)

    # The hosted check runs FIRST and BEFORE any kinoforge import so the
    # subprocess gate works in a stripped-down env (no PYTHONPATH, no
    # `.env` to load) — only `os.environ` is consulted.
    if args.check_hosted:
        missing = _check_hosted_credentials(os.environ.get)
        if missing:
            for var in missing:
                print(f"preflight: missing {var}", file=sys.stderr)
            return 2
        # All four present — fall through to the standard gate so the
        # operator still sees a full pre-live-spend audit.

    from kinoforge.core.dotenv_loader import load_env_file

    env_file = Path(_REPO_ROOT) / ".env"
    if env_file.exists():
        load_env_file(env_file)

    print("preflight: pre-live-spend gate")
    code, lines = run_preflight(
        env_getter=os.environ.get,
        pod_lister=_list_pods_runpod,
        git_dirty=_git_dirty_default,
    )
    for ln in lines:
        print(ln)

    # SkyPilot cluster check — skipped silently when sky SDK is absent
    # (default env). On installed envs (live-skypilot) any active
    # cluster fails preflight with the same severity as a RunPod leak.
    if not _check_no_active_sky_clusters():
        code = 1
        print("  FAIL sky: active SkyPilot clusters present (see stderr)")
    else:
        print("  OK   sky: 0 active clusters (or skypilot not installed)")

    if code == 0:
        print("preflight: PASS — safe to spend")
    else:
        print("preflight: FAIL — fix above before live spend")
    return code


if __name__ == "__main__":
    sys.exit(main())
