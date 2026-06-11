# tests/tools/test_quota_burn_manifest.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.quota_burn_lib import Manifest


def test_manifest_round_trips_through_json(tmp_path: Path) -> None:
    """A Manifest written to JSON reads back equal — bug catch: drift in field order
    or default factories would break teardown idempotence."""
    m = Manifest(
        gcp_vms=["kinoforge-burn-1"],
        gcp_disks=["kinoforge-burn-1-disk"],
        gcp_buckets=["kinoforge-quota-burn-gcp"],
        gcp_budget_id="budget-abc",
        aws_instances=["i-0123456789"],
        aws_volumes=["vol-deadbeef"],
        aws_buckets=["kinoforge-quota-burn-aws-usw2"],
        aws_tables=["kinoforge-quota-burn"],
        aws_budget_name="kinoforge-quota-burn",
        created_at="2026-06-10T09:30:00",
        tag="kinoforge-quota-burn",
    )
    path = tmp_path / "manifest.json"
    m.to_json(path)
    assert Manifest.from_json(path) == m


def test_manifest_from_json_raises_when_missing(tmp_path: Path) -> None:
    """Bug catch: silently returning an empty manifest would cause teardown to no-op,
    leaving live resources running."""
    with pytest.raises(FileNotFoundError):
        Manifest.from_json(tmp_path / "nope.json")


def test_manifest_json_is_pretty_and_sorted(tmp_path: Path) -> None:
    """Sorted keys make manifest diffs reviewable; pretty-printing aids manual inspection."""
    m = Manifest(
        gcp_vms=[],
        gcp_disks=[],
        gcp_buckets=[],
        gcp_budget_id=None,
        aws_instances=[],
        aws_volumes=[],
        aws_buckets=[],
        aws_tables=[],
        aws_budget_name=None,
        created_at="2026-06-10T09:30:00",
        tag="kinoforge-quota-burn",
    )
    path = tmp_path / "m.json"
    m.to_json(path)
    text = path.read_text()
    assert "\n  " in text  # pretty
    parsed_keys = list(json.loads(text).keys())
    assert parsed_keys == sorted(parsed_keys)
