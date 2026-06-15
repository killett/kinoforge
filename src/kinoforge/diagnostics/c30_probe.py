"""C30 probe helpers for fault-isolation of the RunPod restart loop.

Provides direct-GraphQL pod probes, S3 trap-fire counting, verdict
classification, spend-ledger enforcement, and verify-and-retry destroy.
All public helpers are documented in
``docs/superpowers/specs/2026-06-14-c30-restart-loop-diagnosis-design.md``.
"""

from __future__ import annotations
