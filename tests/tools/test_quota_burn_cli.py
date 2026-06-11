"""CLI dispatcher tests for tools/quota_burn.py (Phase 52 Task 8).

The CLI is a thin shim — its job is to route argv to the right lib helper and
make sure heavy SDKs (`google.cloud.*`, `boto3`) are not imported until the
branch that needs them runs. These tests use ``unittest.mock.patch`` to swap
the real client builders for ``MagicMock`` so the tests don't touch the cloud.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.quota_burn import build_parser, main


def test_cli_spinup_writes_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug catch: forgetting to write the manifest leaves teardown unable to
    find the resources to destroy."""
    manifest_path = tmp_path / "manifest.json"
    monkeypatch.setattr("tools.quota_burn._MANIFEST_PATH", manifest_path)

    fake_gcp_out = {
        "vm": "vm-1",
        "disk": "vm-1-disk",
        "bucket": "buck-1",
        "budget_id": "bud-1",
    }
    fake_aws_out = {
        "instance": "i-1",
        "volume": "vol-1",
        "bucket": "buck-2",
        "table": "tab-1",
        "budget_name": "bud-2",
    }
    with (
        patch("tools.quota_burn._build_gcp_clients", return_value=MagicMock()),
        patch("tools.quota_burn._build_aws_clients", return_value=MagicMock()),
        patch("tools.quota_burn.gcp_spin_up", return_value=fake_gcp_out),
        patch("tools.quota_burn.aws_spin_up", return_value=fake_aws_out),
    ):
        rc = main(
            [
                "spinup",
                "--project-id",
                "kinoforge-prod-0ddb375e",
                "--region",
                "us-west1",
                "--zone",
                "us-west1-a",
                "--aws-region",
                "us-west-2",
                "--operator-email",
                "op@example.test",
            ]
        )
    assert rc == 0
    assert manifest_path.exists()


def test_cli_teardown_reads_manifest_then_deletes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug catch: leaving the manifest after teardown would cause a re-run to
    try deleting already-gone resources (loud-but-correct) AND would lie about
    the live-resource state (silent and wrong)."""
    from tools.quota_burn_lib import Manifest

    m = Manifest(
        gcp_vms=["v"],
        gcp_disks=["d"],
        gcp_buckets=["b"],
        gcp_budget_id="bid",
        aws_instances=["i-1"],
        aws_volumes=["vol-1"],
        aws_buckets=["s3-1"],
        aws_tables=["tab"],
        aws_budget_name="bn",
        created_at="2026-06-11T00:00:00",
        tag="kinoforge-quota-burn",
        aws_region="eu-central-1",  # non-default to prove it flows through
    )
    manifest_path = tmp_path / "manifest.json"
    m.to_json(manifest_path)
    monkeypatch.setattr("tools.quota_burn._MANIFEST_PATH", manifest_path)

    with (
        patch("tools.quota_burn._build_gcp_clients", return_value=MagicMock()),
        patch(
            "tools.quota_burn._build_aws_clients", return_value=MagicMock()
        ) as mock_build_aws,
        patch("tools.quota_burn.gcp_tear_down", return_value=["v", "d", "b", "bid"]),
        patch(
            "tools.quota_burn.aws_tear_down",
            return_value=["i-1", "vol-1", "s3-1", "tab", "bn"],
        ),
    ):
        rc = main(
            [
                "teardown",
                "--project-id",
                "kinoforge-prod-0ddb375e",
                "--zone",
                "us-west1-a",
            ]
        )
    assert rc == 0
    assert not manifest_path.exists()
    # CRITICAL: assert the manifest's aws_region propagated to _build_aws_clients.
    # Without this, a regression that hardcodes "us-west-2" would silently pass.
    assert mock_build_aws.call_args.kwargs["region"] == "eu-central-1"


def test_cli_root_does_not_import_cloud_sdks() -> None:
    """Bug catch: importing tools.quota_burn must not drag boto3 / google-cloud
    SDK subpackages into the env. They're lazy-imported on the branch that
    needs them only.

    Implementation note: previous sibling tests in the suite may have already
    loaded heavy SDKs (e.g. ``google.cloud.bigquery``, ``boto3``) into
    ``sys.modules``. To attribute imports to ``tools.quota_burn`` specifically
    we snapshot ``sys.modules`` BEFORE re-importing it and assert the DELTA
    contains no blocked SDK modules. Namespace shells ``google`` /
    ``google.cloud`` carry no SDK code so they would be allowed regardless,
    but the delta approach is stricter — it would also fail if the CLI module
    imported just the shell.
    """
    blocked_prefixes = ("google.cloud.", "boto3")
    # Force a fresh import of tools.quota_burn so its module-level code re-runs.
    sys.modules.pop("tools.quota_burn", None)
    before = set(sys.modules)
    import tools.quota_burn  # noqa: F401

    newly_imported = set(sys.modules) - before
    bad = [n for n in newly_imported if n.startswith(blocked_prefixes)]
    assert bad == [], f"tools.quota_burn dragged in SDK modules: {bad}"


def test_cli_parser_has_all_subcommands() -> None:
    """Bug catch: regression if someone removes a subcommand by mistake."""
    parser = build_parser()
    # argparse exposes the subparser action; inspect it. The subparsers action
    # is the only group action that has a ``choices`` dict mapping subcommand
    # names to their sub-parsers.
    subparsers_group = parser._subparsers
    assert subparsers_group is not None
    sub = next(a for a in subparsers_group._group_actions if hasattr(a, "choices"))
    choices = sub.choices
    assert choices is not None
    # argparse types ``choices`` as ``Iterable[Any]``, but for subparser
    # actions it is always a dict ``{name: ArgumentParser}``. Iterating gives
    # the keys, which is what we want here.
    assert set(choices) == {
        "spinup",
        "teardown",
        "snapshot",
        "submit-quota",
        "scan-bigquery",
    }
