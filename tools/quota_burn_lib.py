# tools/quota_burn_lib.py
"""Quota-burn provisioning + teardown + snapshot helpers.

Builds a paid-utilization billing footprint on AWS + GCP across 5 days so the
GPU quota resubmit on day 5 can cite concrete MTD spend numbers. Every helper
takes injected SDK clients so tests pass fakes; nothing here calls a real
cloud unless the CLI passes a real client.

Design rules:
- Stateless helpers; the manifest persisted at .quota_burn/manifest.json is the
  single source of truth for which tagged resources exist.
- Cloud SDKs lazy-imported in the CLI layer (tools/quota_burn.py), never here.
- Local timezone everywhere; never UTC (per user preference).
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Manifest:
    """Tagged-resource manifest persisted between spinup and teardown."""

    gcp_vms: list[str]
    gcp_disks: list[str]
    gcp_buckets: list[str]
    gcp_budget_id: str | None
    aws_instances: list[str]
    aws_volumes: list[str]
    aws_buckets: list[str]
    aws_tables: list[str]
    aws_budget_name: str | None
    created_at: str
    tag: str = "kinoforge-quota-burn"

    def to_json(self, path: Path) -> None:
        """Write manifest to path as pretty-sorted JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(
            dataclasses.asdict(self),
            indent=2,
            sort_keys=True,
        )
        path.write_text(text + "\n")

    @classmethod
    def from_json(cls, path: Path) -> Manifest:
        """Read manifest from path; raises FileNotFoundError if missing."""
        text = path.read_text()
        data = json.loads(text)
        return cls(**data)
