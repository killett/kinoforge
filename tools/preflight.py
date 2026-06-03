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

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


_REQUIRED_ENV = (
    "KINOFORGE_LIVE_TESTS",
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


def main() -> int:
    """CLI entrypoint: load ``.env``, run checks against real defaults, print, exit.

    ``.env`` load is silent if the file is absent. Returns the exit
    code from :func:`run_preflight` (``0`` on full pass).
    """
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
    if code == 0:
        print("preflight: PASS — safe to spend")
    else:
        print("preflight: FAIL — fix above before live spend")
    return code


if __name__ == "__main__":
    sys.exit(main())
