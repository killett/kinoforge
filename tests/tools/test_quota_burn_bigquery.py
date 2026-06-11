from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from tools.quota_burn_lib import BigQueryCapExceeded, bq_scan_with_cap


@dataclass
class FakeBqJob:
    total_bytes_processed: int
    rows: int

    def result(self) -> list[dict[str, Any]]:
        return [{"x": i} for i in range(self.rows)]


@dataclass
class FakeBqClient:
    dry_run_bytes: int
    live_rows: int
    queries: list[dict[str, Any]] = field(default_factory=list)

    def query(self, sql: str, **kwargs: Any) -> FakeBqJob:
        self.queries.append({"sql": sql, **kwargs})
        if kwargs.get("dry_run", False) or (
            kwargs.get("job_config") and getattr(kwargs["job_config"], "dry_run", False)
        ):
            return FakeBqJob(total_bytes_processed=self.dry_run_bytes, rows=0)
        return FakeBqJob(total_bytes_processed=self.dry_run_bytes, rows=self.live_rows)


def test_bq_scan_with_cap_runs_when_under_cap() -> None:
    """Bug catch: dry-run gate must pass through and run live query when bytes are under cap."""
    client = FakeBqClient(dry_run_bytes=5_000_000_000, live_rows=10)
    out = bq_scan_with_cap(
        client, sql="SELECT * FROM `t`", max_bytes_billed=10_000_000_000
    )
    assert out == {"rows": 10, "bytes_billed": 5_000_000_000}
    assert len(client.queries) == 2  # dry-run + live
    live_cfg = client.queries[1].get("job_config")
    assert live_cfg is not None
    assert getattr(live_cfg, "maximum_bytes_billed", None) == 10_000_000_000


def test_bq_scan_with_cap_blocks_when_over_cap() -> None:
    """Bug catch: a runaway BigQuery scan (R5) is the only way the burn can
    blow past $20. The dry-run gate must REFUSE before any billable query."""
    client = FakeBqClient(dry_run_bytes=50_000_000_000, live_rows=999_999)
    with pytest.raises(BigQueryCapExceeded, match="50000000000"):
        bq_scan_with_cap(
            client, sql="SELECT * FROM `huge`", max_bytes_billed=10_000_000_000
        )
    # Only the dry-run fired, not the live query.
    assert len(client.queries) == 1
