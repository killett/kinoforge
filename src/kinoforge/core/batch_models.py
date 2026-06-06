"""Batch manifest + outcome dataclasses (extracted from core/batch.py).

This module exists so that core/batch_events.py can import BatchEntry
without a cycle with core/batch.py.  Keep it dependency-light: pydantic
+ stdlib only; no kinoforge.core.* imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
        keyframe: Per-entry keyframe overrides shallow-merged onto
            cfg.keyframe (entry wins per key).  Only the fields present
            in this dict are overridden; omitted fields fall back to
            cfg.keyframe defaults.  Ignored when cfg.keyframe is None.
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str | None = None
    prompt_file: str | None = None
    mode: str
    run_id: str | None = None
    params: dict[str, Any] | None = None
    spec: dict[str, Any] | None = None
    assets: list[dict[str, Any]] | None = None
    keyframe: dict[str, Any] | None = None

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


@dataclass
class BatchOutcome:
    """The result of one entry in a batch run.

    Attributes:
        run_id: The entry's run_id (always set after load_manifest).
        status: One of "ok" / "fail" / "aborted" / "interrupted".
        duration_s: Seconds the entry was in-flight.  ``0.0`` for
            "aborted" entries (sweep path, never started); actual
            wall-clock for "ok" / "fail" / "interrupted".  Only ``None``
            on the narrow ``_finalize_summary`` backstop path, when an
            entry was never even scheduled (deploy_session raised
            before the loop).
        uri: Persisted artifact URI on "ok"; None otherwise.
        error: Stringified exception on "fail" / "interrupted" /
            "aborted" (for the latter two, formatted as
            ``"batch aborted by <FatalType>"``); None on "ok" and on
            the rare ``_finalize_summary`` backstop path.
    """

    run_id: str
    status: Literal["ok", "fail", "aborted", "interrupted"]
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
