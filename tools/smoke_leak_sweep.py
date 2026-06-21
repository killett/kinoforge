"""Layer-3 leak-detection sweep.

Reaps any RunPod pod whose age exceeds the per-tag ceiling. Designed
to run every 30 min via .github/workflows/leak-sweep.yml + on-demand
via ``pixi run smoke-leak-sweep``.

The watchdog is INDEPENDENT of the smoke tiers — when a smoke crash
defeats its own finally block (T22 attempt 2 lost $0.63 this way),
this cron catches the leak within 30-60 min and produces a GitHub
issue with pod_id + age + spend + tag.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from typing import Any

_AGE_BUDGET: dict[str | None, float] = {
    "kinoforge-smoke-tier-3": 0.75,  # 45 min ceiling
    "kinoforge-smoke-tier-4": 1.50,  # 90 min ceiling
    None: 4.00,  # untagged: 4h ceiling
}

_log = logging.getLogger("smoke_leak_sweep")


def _get_runpod_provider() -> Any:  # noqa: ANN401 — provider has no public alias
    """Test-seam — overridden in unit tests."""
    from kinoforge.core import registry as kf_registry
    from kinoforge.providers import runpod  # noqa: F401

    return kf_registry.get_provider("runpod")()


def _post_issue(*, pod_id: str, tag: str | None, age_h: float, spend: float) -> None:
    """Post a GitHub issue via gh CLI. Auth via GITHUB_TOKEN."""
    title = f"smoke leak: pod {pod_id} reaped after {age_h:.1f}h ({tag or 'untagged'})"
    ceiling = _AGE_BUDGET.get(tag, _AGE_BUDGET[None])
    body = (
        f"## Reaped pod\n"
        f"- pod_id: `{pod_id}`\n"
        f"- smoke_tier tag: `{tag or 'untagged'}`\n"
        f"- age at reap: `{age_h:.2f}h`\n"
        f"- estimated spend: `${spend:.2f}`\n\n"
        f"This pod exceeded the {ceiling:.2f}h ceiling for its tier. "
        f"The originating smoke either crashed before its `finally` "
        f"block or did not tag the pod. Investigate the workflow run "
        f"that owned this pod and add the missing tag / fix the "
        f"finally / harden the harness."
    )
    cmd = [
        "gh",
        "issue",
        "create",
        "--title",
        title,
        "--body",
        body,
        "--label",
        "leaked-smoke-pod",
    ]  # noqa: S607 — gh on $PATH; resolved at runtime
    subprocess.run(cmd, check=False, timeout=60)  # noqa: S603


def main(argv: list[str] | None = None) -> int:
    """Entry point — list, age-check, destroy + issue per leaked pod."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log intentions; do not destroy or post issues",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    try:
        provider = _get_runpod_provider()
        pods = list(provider.list_instances())
    except Exception as exc:  # noqa: BLE001
        _log.error("failed to list pods: %r", exc)
        return 1

    now = time.time()
    for pod in pods:
        tag = pod.tags.get("smoke_tier")
        budget = _AGE_BUDGET.get(tag, _AGE_BUDGET[None])
        age_h = (now - pod.created_at) / 3600.0
        if age_h <= budget:
            _log.info(
                "OK pod=%s tag=%s age=%.2fh (budget %.2fh)",
                pod.id,
                tag,
                age_h,
                budget,
            )
            continue
        spend = age_h * pod.cost_rate_usd_per_hr
        _log.warning(
            "REAP pod=%s tag=%s age=%.2fh spend=$%.2f",
            pod.id,
            tag,
            age_h,
            spend,
        )
        if args.dry_run:
            continue
        try:
            provider.destroy_instance(pod.id)
        except Exception as exc:  # noqa: BLE001
            _log.error("destroy failed for %s: %r", pod.id, exc)
            continue
        _post_issue(pod_id=pod.id, tag=tag, age_h=age_h, spend=spend)
    return 0


if __name__ == "__main__":
    sys.exit(main())
