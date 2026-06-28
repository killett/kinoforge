"""RuntimeProbe — single liveness snapshot for a provider-side pod.

Used by sweeper-ephemeral-reap (spec 2026-06-28) when the ephemeral
index gives us a pod_id but no heartbeat history. The provider's
``probe_runtime`` returns a RuntimeProbe; sweeper synthesises a
ledger-shape entry from it for the classify path.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeProbe:
    """Live runtime snapshot for one pod, fetched via provider API.

    Attributes:
        pod_id: Provider pod identifier.
        found: False when the provider returned 404 / "pod gone".
        container_uptime_s: Seconds since container start; None if
            not available (early boot, found=False, or partial probe).
        gpu_util_pct: MAX of per-GPU utilisation percent; None if
            no GPU array reported.
        cpu_pct: Container CPU percent; None if not reported.
        cost_per_hr: Optional cost/hour for future cost-cache reuse.
        probed_at_local: ISO-format local-TZ timestamp (per project rule).
        error: Optional WARN payload when found=True but probe partial.
    """

    pod_id: str
    found: bool
    container_uptime_s: float | None
    gpu_util_pct: float | None
    cpu_pct: float | None
    cost_per_hr: float | None
    probed_at_local: str
    error: str | None = None
