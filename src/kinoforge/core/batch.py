"""Batch generation: manifest schema + dispatch (Layer L).

This module owns:
  * BatchEntry / BatchManifest pydantic models with strict validation.
  * load_manifest() — reads YAML, resolves prompt_file paths, auto-indexes
    run_ids, returns a fully validated BatchManifest.
  * BatchOutcome / BatchResult dataclasses.
  * batch_generate() — the orchestration entry point (added in Task 3).

Core-import-ban: this module imports ONLY from kinoforge.core.* + stdlib
+ pydantic + PyYAML.  No kinoforge.providers / engines / sources.  The
invariant test in tests/test_core_invariant.py enforces this via
subprocess isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from kinoforge.core.errors import ConfigError


class BatchEntry(BaseModel):
    """One entry in a batch manifest.

    Attributes:
        prompt: Inline prompt text. Mutually exclusive with prompt_file.
        prompt_file: Path to a text file (resolved relative to the
            manifest's parent dir). Mutually exclusive with prompt.
            After load_manifest runs, this is always None — the loader
            collapses prompt_file into prompt.
        mode: Generation mode (t2v / i2v / flf2v). Required per entry —
            no inherited default. An explicit per-entry choice avoids
            silent mode mixups.
        run_id: Sub-namespace under the batch_id for this entry's
            artifacts. None means "let the loader auto-index by position".
            After load_manifest runs, this is always set.
        params: Engine-neutral param overrides shallow-merged onto
            cfg.params (entry wins per key).
        spec: Engine-interpreted spec overrides shallow-merged onto
            cfg.spec (entry wins per key).
        assets: List of asset dicts forwarded into GenerationRequest.assets.
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str | None = None
    prompt_file: str | None = None
    mode: str
    run_id: str | None = None
    params: dict[str, Any] | None = None
    spec: dict[str, Any] | None = None
    assets: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def _exactly_one_prompt_source(self) -> BatchEntry:
        """Reject entries that set both or neither of prompt / prompt_file.

        Returns:
            ``self`` unchanged when the rule is satisfied.

        Raises:
            ValueError: When neither or both of prompt / prompt_file are set.
        """
        if (self.prompt is None) == (self.prompt_file is None):
            raise ValueError("entry must set exactly one of `prompt` / `prompt_file`")
        return self


class BatchManifest(BaseModel):
    """A validated batch manifest.

    Attributes:
        entries: One or more BatchEntry objects, in submission order.
    """

    model_config = ConfigDict(extra="forbid")

    entries: list[BatchEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_run_ids(self) -> BatchManifest:
        """Reject manifests whose explicit run_ids collide.

        When ANY entry sets ``run_id`` explicitly, ALL run_ids (including
        the auto-derived ones added by load_manifest later) must be
        unique.  When NONE set ``run_id``, the loader auto-indexes
        ``"0"``, ``"1"``, ... — collision-free by construction.

        Returns:
            ``self`` unchanged when run_ids are unique.

        Raises:
            ValueError: When the explicit run_id set contains duplicates.
        """
        ids = [e.run_id for e in self.entries if e.run_id is not None]
        if ids and len(set(ids)) != len(ids):
            dupes = sorted({x for x in ids if ids.count(x) > 1})
            raise ValueError(f"duplicate run_id in manifest: {dupes}")
        return self


def load_manifest(path: Path) -> BatchManifest:
    """Load and fully validate a batch manifest YAML.

    Performs:
      1. YAML parse (top-level must be a list).
      2. pydantic validation (extra="forbid", per-entry exactly-one
         prompt source, manifest-level run_id uniqueness).
      3. prompt_file resolution against ``path.parent`` + content read
         + ``.strip()`` of trailing whitespace.
      4. Auto-indexing of any entry that didn't set run_id.

    After this returns, every entry has ``prompt is not None``,
    ``prompt_file is None``, ``run_id is not None``.

    Args:
        path: Filesystem path to the manifest YAML.

    Returns:
        A fully validated BatchManifest ready for batch_generate().

    Raises:
        ConfigError: Manifest isn't a YAML list, or a prompt_file is missing.
        pydantic.ValidationError: Schema / per-entry / manifest-level rules.
    """
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, list):
        raise ConfigError("manifest top-level must be a YAML list of entries")
    manifest = BatchManifest(entries=raw)

    base = path.parent
    for entry in manifest.entries:
        if entry.prompt_file is not None:
            resolved = (base / entry.prompt_file).resolve()
            if not resolved.is_file():
                raise ConfigError(
                    f"prompt_file not found: {resolved} (entry mode={entry.mode!r})"
                )
            entry.prompt = resolved.read_text().strip()
            entry.prompt_file = None

    for idx, entry in enumerate(manifest.entries):
        if entry.run_id is None:
            entry.run_id = str(idx)

    return manifest


@dataclass
class BatchOutcome:
    """The result of one entry in a batch run.

    Attributes:
        run_id: The entry's run_id (always set after load_manifest).
        status: One of "ok" / "fail" / "aborted" / "interrupted".
        duration_s: Seconds the entry was in-flight (None for "aborted").
        uri: Persisted artifact URI on "ok"; None otherwise.
        error: Stringified exception on "fail" / "interrupted"; None otherwise.
    """

    run_id: str
    status: str
    duration_s: float | None = None
    uri: str | None = None
    error: str | None = None


@dataclass
class BatchResult:
    """Summary of one batch_generate() call.

    Attributes:
        batch_id: The batch namespace ID (e.g. "batch-20260531-093052").
        started_at: ISO local-tz timestamp string.
        finished_at: ISO local-tz timestamp string.
        outcomes: Ordered by entry submission order (NOT completion order).
    """

    batch_id: str
    started_at: str
    finished_at: str
    outcomes: list[BatchOutcome] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-friendly shape written to ``_batch_summary.json``.

        Returns:
            A dict with ``batch_id``, ``started_at``, ``finished_at``,
            and ``entries`` (the outcomes with ``None`` fields omitted).
        """
        return {
            "batch_id": self.batch_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "entries": [
                {k: v for k, v in vars(o).items() if v is not None}
                for o in self.outcomes
            ],
        }
