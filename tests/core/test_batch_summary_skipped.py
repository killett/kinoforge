"""Tests for batch_generate _batch_summary.json canonical redaction.

The EphemeralSession skip path (policy.batch_summary_write=False) is
exercised in test_ephemeral_run_cleanup.py once Task 14 lands. This file
pins the always-on policy: vault-loaded → summary file written but every
prompt token comes out as a placeholder.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# Imports below cascade-trigger adapter self-registration the same way
# tests/core/test_batch_generate.py wires it.
import kinoforge.engines.fake  # noqa: F401
import kinoforge.providers.local  # noqa: F401
import kinoforge.sources.http  # noqa: F401
from kinoforge.core.batch import BatchEntry, BatchManifest, batch_generate
from kinoforge.core.redaction import RedactionRegistry
from kinoforge.providers.local import LocalProvider
from kinoforge.stores.local import LocalArtifactStore
from tests.core._fakes import _BatchSpyEngine
from tests.core.test_orchestrator import _compute_cfg, _probe_profile


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    RedactionRegistry.instance().clear_session()
    yield
    RedactionRegistry.instance().clear_session()


def _spy_engine(**kwargs: Any) -> _BatchSpyEngine:
    return _BatchSpyEngine(
        probe_profile=_probe_profile(),
        declared_flags_map={},
        required_spec_keys=set(),
        **kwargs,
    )


def _two_entry_manifest(prompt_a: str, prompt_b: str) -> BatchManifest:
    return BatchManifest(
        entries=[
            BatchEntry(prompt=prompt_a, mode="t2v", run_id="a"),
            BatchEntry(prompt=prompt_b, mode="t2v", run_id="b"),
        ]
    )


def _run_batch(tmp_path: Path, prompts: tuple[str, str]) -> Path:
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    batch_generate(
        cfg,
        _two_entry_manifest(*prompts),
        store=store,
        batch_id="b",
        engine=_spy_engine(),
        provider=LocalProvider(),
        state_dir=tmp_path / "_state",
    )
    return tmp_path / "b" / "_batch_summary.json"


def test_summary_written_in_default_mode(tmp_path: Path) -> None:
    """Empty registry, no vault: _batch_summary.json present + readable."""
    summary_path = _run_batch(tmp_path, ("alpha", "beta"))
    assert summary_path.is_file()
    payload = json.loads(summary_path.read_text())
    assert payload["batch_id"] == "b"


def test_summary_redacts_registered_substring_in_uri(tmp_path: Path) -> None:
    """A registered token that lands in any persisted string (uri / run_id /
    batch_id / error) gets substituted with a placeholder.

    BatchSummary.to_dict() does not currently carry prompt bodies, but the
    canonical redaction call must still wrap every persisted summary so
    future fields (e.g. run-tag breadcrumbs) cannot regress on this
    invariant. Test registers the batch_id substring and asserts the
    placeholder lands in the persisted file.

    Would-fail-bug: writing summary.to_dict() directly to put_json would
    let any future field carrying a vault-registered token leak into
    _batch_summary.json.
    """
    RedactionRegistry.instance().add("redacted-id", kind="prompt:positive")
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    batch_generate(
        cfg,
        _two_entry_manifest("alpha", "beta"),
        store=store,
        batch_id="redacted-id",
        engine=_spy_engine(),
        provider=LocalProvider(),
        state_dir=tmp_path / "_state",
    )
    summary_path = tmp_path / "redacted-id" / "_batch_summary.json"
    assert summary_path.is_file()
    persisted = summary_path.read_text()
    assert "redacted-id" not in persisted
    assert "<prompt:positive:" in persisted


def test_summary_redaction_does_not_corrupt_json(tmp_path: Path) -> None:
    """Redacted summary must still round-trip through json.load."""
    RedactionRegistry.instance().add("redacted-id", kind="prompt:positive")
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    batch_generate(
        cfg,
        _two_entry_manifest("alpha", "beta"),
        store=store,
        batch_id="redacted-id",
        engine=_spy_engine(),
        provider=LocalProvider(),
        state_dir=tmp_path / "_state",
    )
    summary_path = tmp_path / "redacted-id" / "_batch_summary.json"
    payload = json.loads(summary_path.read_text())
    assert isinstance(payload, dict)
    run_ids = {entry["run_id"] for entry in payload["entries"]}
    assert run_ids == {"a", "b"}


def test_summary_passthrough_when_registry_empty(tmp_path: Path) -> None:
    """Public-by-design: batch_id stays plain in the summary."""
    summary_path = _run_batch(tmp_path, ("alpha-public", "beta-public"))
    persisted = summary_path.read_text()
    assert '"batch_id": "b"' in persisted
