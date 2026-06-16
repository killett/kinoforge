# ruff: noqa: D103
"""Pretty-print a per-iteration summary of the uptime-field sweep log."""

import json
from pathlib import Path

LOG = Path(__file__).resolve().parent / "_uptime_field_sweep_log.jsonl"


def main() -> None:
    total_spend = 0.0
    for line in LOG.open():
        r = json.loads(line)
        i = r.get("iteration")
        spend = r.get("est_spend_usd") or 0.0
        total_spend += spend
        gpu = r.get("gpu_id")
        ct = r.get("cloud_type")
        cents = r.get("cents_per_hr")
        err = r.get("error")
        if err:
            print(f"  iter {i:>2}: ERROR {err} ({gpu} {ct} @ {cents}c/hr)")
            continue
        s = r.get("summary") or {}
        rt_mono = s.get("runtime_field_monotonic")
        rt_neg = s.get("runtime_field_any_negative")
        rt_null = s.get("runtime_field_any_null")
        rt_zero = s.get("runtime_field_always_zero")
        rt_diff = s.get("runtime_field_max_disagreement_s")
        top_zero = s.get("top_field_always_zero")
        top_diff = s.get("top_field_max_disagreement_s")
        print(f"  iter {i:>2}: {gpu:<35} {ct:<10} {cents:>3}c/hr  ${spend:.4f}")
        rt_summary = (
            "OK (monotonic, no anomalies)"
            if rt_mono and not rt_neg and not rt_null and not rt_zero
            else f"mono={rt_mono} neg={rt_neg} null={rt_null} zero={rt_zero} max_diff={rt_diff}"
        )
        top_summary = (
            f"always_zero (max_diff={top_diff:.1f}s)"
            if top_zero and top_diff
            else f"zero={top_zero} max_diff={top_diff}"
        )
        print(f"        runtime.uptimeInSeconds: {rt_summary}")
        print(f"        Pod.uptimeSeconds:       {top_summary}")
    print()
    print(f"  cumulative spend: ${total_spend:.4f}")


if __name__ == "__main__":
    main()
