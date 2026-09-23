"""Task-11 live-run util probe (operator script for the H3 LoRA live proof).

Reads the lifecycle ledger for the Modal instance, resolves its public
``.modal.run`` endpoint, and prints a one-line util snapshot plus (optionally)
the raw ``/health`` and ``/lora/inventory`` bodies. Exists to satisfy the
CLAUDE.md "poll utilisation, never est_spend" rule during a live smoke, where
the orchestrator runs detached and the controlling agent must probe it from a
separate short-lived process.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

LEDGER = Path("/workspace/.kinoforge/_lifecycle/ledger.json")
EPHEMERAL = Path("/workspace/.kinoforge/_lifecycle/ephemeral-index.json")


def _entries() -> list[dict[str, Any]]:
    """Return every ledger + ephemeral-index row, oldest first.

    Returns:
        Row mappings; malformed or missing files contribute nothing.
    """
    out: list[dict[str, Any]] = []
    for p in (LEDGER, EPHEMERAL):
        if not p.exists():
            continue
        try:
            parsed: Any = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        rows = parsed.get("entries") if isinstance(parsed, dict) else parsed
        if isinstance(rows, list):
            out.extend(r for r in rows if isinstance(r, dict))
    return out


def resolve(instance_id: str | None = None) -> tuple[str | None, str | None]:
    """Resolve an instance id to its public HTTP endpoint.

    Args:
        instance_id: Specific id to look for; ``None`` takes the newest row.

    Returns:
        ``(instance_id, endpoint_url)``; the url is ``None`` when the pod has
        not been recorded with an endpoint yet.
    """
    for r in reversed(_entries()):
        rid = r.get("id") or r.get("instance_id")
        if instance_id and rid != instance_id:
            continue
        eps = r.get("endpoints") or {}
        url = eps.get("http") or next(
            (v for v in eps.values() if isinstance(v, str) and v.startswith("http")),
            None,
        )
        if url:
            return str(rid), str(url)
    return (instance_id, None)


def _get(url: str, timeout: float = 15.0) -> tuple[int, str]:
    """GET ``url``, returning ``(status, body)``; status 0 on transport error.

    Args:
        url: Absolute http(s) URL.
        timeout: Socket timeout in seconds.

    Returns:
        The HTTP status (0 when the request never completed) and the body text.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return int(resp.status), resp.read().decode()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode()[:400]
    except OSError as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def main() -> int:
    """Print one util snapshot (and optional /health, /lora/inventory).

    Returns:
        Process exit code; always 0 — a probe never fails the caller.
    """
    argv = sys.argv[1:]
    want = argv[0] if argv and not argv[0].startswith("-") else None
    iid, url = resolve(want)
    ts = datetime.now().strftime("%H:%M:%S")
    if not url:
        print(f"[{ts}] no modal endpoint in ledger yet (id={iid}) — provisioning")
        return 0
    print(f"[{ts}] id={iid} url={url}")
    status, body = _get(url.rstrip("/") + "/util")
    if status != 200:
        print(f"[{ts}] /util HTTP {status}: {body[:300]}")
    else:
        d = json.loads(body)
        print(
            f"[{ts}] UTIL gpuUtilPercent={d.get('gpu_util_percent')} "
            f"cpuPercent={d.get('cpu_percent')} "
            f"memoryPercent={d.get('memory_percent')} "
            f"diskPercent={d.get('disk_percent')} "
            f"uptime_s={d.get('uptime_seconds')}"
        )
    for flag, route in (("--health", "/health"), ("--inventory", "/lora/inventory")):
        if flag in argv:
            hs, hb = _get(url.rstrip("/") + route)
            print(f"[{ts}] {route} HTTP {hs}: {hb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
