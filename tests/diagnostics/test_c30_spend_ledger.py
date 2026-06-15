"""Unit tests for ``c30_probe.assert_under_cap`` + ``append_spend_entry``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kinoforge.diagnostics.c30_probe import (
    BudgetCapExceeded,
    append_spend_entry,
    assert_under_cap,
)


def _seed(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload))


def test_missing_file_is_zero(tmp_path: Path) -> None:
    assert_under_cap(tmp_path / "absent.json", hard_cap_usd=1.50)


def test_under_cap_does_not_raise(tmp_path: Path) -> None:
    p = tmp_path / "l.json"
    _seed(p, {"cumulative_usd": 0.30, "entries": []})
    assert_under_cap(p, hard_cap_usd=1.50)


def test_at_or_above_cap_raises(tmp_path: Path) -> None:
    p = tmp_path / "l.json"
    _seed(p, {"cumulative_usd": 1.50, "entries": []})
    with pytest.raises(BudgetCapExceeded):
        assert_under_cap(p, hard_cap_usd=1.50)


def test_malformed_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "l.json"
    p.write_text("{this is not json")
    with pytest.raises(ValueError):
        assert_under_cap(p, hard_cap_usd=1.50)


def test_append_accumulates(tmp_path: Path) -> None:
    p = tmp_path / "l.json"
    _seed(p, {"cumulative_usd": 0.10, "entries": []})
    append_spend_entry(
        p,
        {
            "phase": "a1a",
            "pod_id": "pod-1",
            "gpu_type_id": "RTXA2000",
            "cents_per_hr": 10,
            "start_ts": "2026-06-14T10:00:00-07:00",
            "end_ts": "2026-06-14T10:10:00-07:00",
            "est_spend_usd": 0.017,
        },
    )
    payload = json.loads(p.read_text())
    assert payload["cumulative_usd"] == pytest.approx(0.117)
    assert payload["entries"][-1]["phase"] == "a1a"


def test_append_refuses_non_monotonic(tmp_path: Path) -> None:
    p = tmp_path / "l.json"
    _seed(
        p,
        {
            "cumulative_usd": 0.0,
            "entries": [
                {
                    "phase": "x",
                    "pod_id": "p",
                    "gpu_type_id": "g",
                    "cents_per_hr": 1,
                    "start_ts": "2026-06-14T10:00:00-07:00",
                    "end_ts": "2026-06-14T10:10:00-07:00",
                    "est_spend_usd": 0.0,
                }
            ],
        },
    )
    with pytest.raises(ValueError, match="monotonic"):
        append_spend_entry(
            p,
            {
                "phase": "y",
                "pod_id": "p2",
                "gpu_type_id": "g",
                "cents_per_hr": 1,
                "start_ts": "2026-06-14T09:00:00-07:00",
                "end_ts": "2026-06-14T09:10:00-07:00",
                "est_spend_usd": 0.0,
            },
        )
