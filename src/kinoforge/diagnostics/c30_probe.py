"""C30 probe helpers for fault-isolation of the RunPod restart loop.

Provides direct-GraphQL pod probes, S3 trap-fire counting, verdict
classification, spend-ledger enforcement, and verify-and-retry destroy.
All public helpers are documented in
``docs/superpowers/specs/2026-06-14-c30-restart-loop-diagnosis-design.md``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

_DIAG_KEY_PATTERN = re.compile(r"/diag-\d{8}T\d{6}Z\.txt$")


class Verdict(Enum):
    """Outcome classes for a 10-minute probe window.

    SURVIVED  — pod stayed up the whole window; no trap fires; uptime
                monotonically increased across all samples.
    RESTARTED — pod cycled >=3 times within the window (trap-fire count
                is the authoritative signal; uptime drops corroborate).
    AMBIGUOUS — evidence cannot distinguish the two; rerun the probe
                or treat as RESTARTED conservatively per spec §3.
    """

    SURVIVED = "survived"
    RESTARTED = "restarted"
    AMBIGUOUS = "ambiguous"


def classify_run(
    poll_trail: Sequence[tuple[float, int | None]],
    fire_count: int,
) -> Verdict:
    """Classify a probe run from its poll trail and S3 trap-fire count.

    Args:
        poll_trail: ``(elapsed_seconds, uptime_in_seconds)`` per sample.
            ``uptime_in_seconds`` may be ``None`` when the GraphQL
            ``pod(podId)`` response lacked a ``runtime`` block (transient).
        fire_count: Number of ``diag-*.txt`` objects under the run's
            S3 prefix.

    Returns:
        Verdict per spec §3 rules.
    """
    if fire_count >= 3:
        return Verdict.RESTARTED
    if fire_count >= 1:
        return Verdict.AMBIGUOUS
    if len(poll_trail) < 2:
        return Verdict.AMBIGUOUS
    raw = [u for _, u in poll_trail]
    if any(u is None for u in raw):
        return Verdict.AMBIGUOUS
    uptimes: list[int] = [u for u in raw if u is not None]
    for prev, curr in zip(uptimes, uptimes[1:], strict=False):
        if curr <= prev:
            return Verdict.AMBIGUOUS
    return Verdict.SURVIVED


def count_trap_fires(
    s3_client: Any,  # noqa: ANN401 — injected boto3 S3 client; avoid SDK import in signature
    bucket: str,
    prefix: str,
) -> int:
    """Count ``diag-YYYYMMDDTHHMMSSZ.txt`` objects under ``bucket/prefix``.

    Args:
        s3_client: A boto3 S3 client (or anything with a compatible
            ``list_objects_v2`` method).
        bucket: S3 bucket name (no scheme).
        prefix: Key prefix. Must include the trailing slash if the
            prefix is a directory.

    Returns:
        Number of diag-pattern objects. Returns 0 on ``NoSuchKey``.
    """
    total = 0
    continuation: str | None = None
    try:
        while True:
            kw: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if continuation is not None:
                kw["ContinuationToken"] = continuation
            page = s3_client.list_objects_v2(**kw)
            for obj in page.get("Contents", []) or []:
                key = obj.get("Key", "")
                if _DIAG_KEY_PATTERN.search(key):
                    total += 1
            if not page.get("IsTruncated"):
                return total
            continuation = page.get("NextContinuationToken")
            if continuation is None:
                return total
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchKey":
            return 0
        raise


class BudgetCapExceeded(RuntimeError):
    """Raised when cumulative spend would meet or exceed the hard cap."""


def _read_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"cumulative_usd": 0.0, "entries": []}
    try:
        return json.loads(path.read_text())  # type: ignore[no-any-return]
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed C30 spend ledger at {path}: {exc}") from exc


def assert_under_cap(path: Path, hard_cap_usd: float) -> None:
    """Raise ``BudgetCapExceeded`` if cumulative spend in ``path`` >= cap."""
    payload = _read_ledger(path)
    cumulative = float(payload.get("cumulative_usd", 0.0))
    if cumulative >= hard_cap_usd:
        raise BudgetCapExceeded(
            f"Cumulative C30 spend ${cumulative:.4f} >= cap ${hard_cap_usd:.2f}"
        )


def append_spend_entry(path: Path, entry: dict[str, Any]) -> None:
    """Append a spend entry and rewrite the ledger.

    Args:
        path: Ledger JSON path.
        entry: Dict with keys ``phase``, ``pod_id``, ``gpu_type_id``,
            ``cents_per_hr``, ``start_ts``, ``end_ts``, ``est_spend_usd``.
            Timestamps must be ISO-8601 with offset.

    Raises:
        ValueError: If ``start_ts`` precedes the last existing entry's
            ``end_ts``.
    """
    payload = _read_ledger(path)
    entries = list(payload.get("entries", []))
    if entries:
        last_end = datetime.fromisoformat(str(entries[-1]["end_ts"]))
        new_start = datetime.fromisoformat(str(entry["start_ts"]))
        if new_start < last_end:
            raise ValueError(
                f"Entry start_ts {entry['start_ts']} is not monotonic vs "
                f"prior entry end_ts {entries[-1]['end_ts']}"
            )
    entries.append(entry)
    cumulative = float(payload.get("cumulative_usd", 0.0)) + float(
        entry["est_spend_usd"]
    )
    path.write_text(
        json.dumps(
            {"cumulative_usd": round(cumulative, 6), "entries": entries},
            indent=2,
        )
        + "\n"
    )
