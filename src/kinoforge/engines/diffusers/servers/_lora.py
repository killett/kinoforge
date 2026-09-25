"""Shared LoRA seam for the diffusers pod servers.

Runs INSIDE the pod, where ``kinoforge.core`` does NOT exist — the whole
``servers/`` package is base64-embedded into the boot script, so this module
imports stdlib plus the two packages every pod server already runs on,
``fastapi`` and ``pydantic``. Nothing from ``kinoforge.*`` may ever be imported
here; ``_av_io`` and ``_util_stats`` are the siblings under the same rule.

The mechanism here knows nothing model-specific. Everything that differs
between Wan and MiniMax-H3 — which submodule a LoRA loads into, how the load
call is spelled, what fixup the model needs once every adapter is attached,
how to explain a load failure — arrives as DATA on a :class:`LoraProfile`:
small callables supplied by the model's server after its pipeline is built.
No subclasses, no registry, no imports in either direction.

Two invariants carry the weight:

* **Replace, never accumulate.** Every ``apply_stack`` starts by unloading, so
  applying ``[A]`` then ``[B]`` leaves exactly ``[B]``; a skipped unload blends
  A and B silently while the inventory reads ``[B]``.
* **All or nothing.** A failure on entry *k* unloads whatever landed and empties
  the inventory before re-raising. A half-applied stack that still reports a
  full inventory is the worst outcome available: the controller believes a LoRA
  is active that is not, and the defect only ever shows up in the pixels.

:func:`build_lora_router` puts the HTTP contract in front of that mechanism —
the same three endpoints ``DiffusersBackend.set_lora_stack`` already speaks to
the Wan server, so one client serves both pods.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_log = logging.getLogger(__name__)

# Anything outside this set becomes "_" in a per-ref directory name, so a ref
# can never contribute a path separator or a ".." segment.
_UNSAFE_REF_CHARS = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class LoraProfile:
    """Per-model LoRA behaviour, as data. Built after the pipeline loads.

    Attributes:
        name: Model identity, used only in error messages.
        targets: Every legal target name for this pipeline, in a stable order.
            A single-partition model has one; MiniMax-H3 has two.
        default_target: Target used by an entry that names none, or ``None``
            when the model refuses to guess (see :class:`TargetRequired`).
        load: ``(pipe, path, adapter_name, target) -> None``. Attaches one
            adapter. The profile owns the whole spelling of the diffusers call.
        module_for: ``(pipe, target) -> module``. Returns the submodule whose
            ``set_adapters`` governs that target's adapters.
        after_load: ``(pipe) -> None``. Whole-model fixup run ONCE, after every
            entry has loaded — never per entry. H3 uses it to restore bf16,
            which its fp32 LoRA files would otherwise drag the unfused path
            into for the rest of the pod's life.
        explain_load_failure: ``(exc) -> hint | None``. Turns a diffusers
            traceback into an operator-readable cause; the hint rides on the
            raised :class:`LoraLoadError`.
    """

    name: str
    targets: tuple[str, ...]
    default_target: str | None
    load: Callable[[Any, str, str, str], None]
    module_for: Callable[[Any, str], Any]
    after_load: Callable[[Any], None]
    explain_load_failure: Callable[[BaseException], str | None]


@dataclass(frozen=True)
class ResolvedEntry:
    """One stack entry whose bytes are already on disk."""

    ref: str
    path: Path
    strength: float
    target: str | None


@dataclass(frozen=True)
class InventoryEntry:
    """One row of the pod's LoRA inventory (wire shape lives in the router)."""

    ref: str
    filename: str
    size_bytes: int
    adapter_name: str
    strength: float
    target: str


class TargetRequired(Exception):
    """Raised when an entry declares no target and the profile has no default."""


class LoraLoadError(Exception):
    """A load failed. Carries the offending ref and the profile's hint."""

    def __init__(self, ref: str, hint: str | None, underlying: BaseException) -> None:
        """Record the failed ref, the profile's hint, and the original cause."""
        super().__init__(f"loading {ref} failed: {underlying}")
        self.ref = ref
        self.hint = hint
        self.underlying = underlying


class DownloadSpec(Protocol):
    """Structural shape of the orchestrator's pre-resolved download instruction.

    A Protocol rather than a concrete model because the pydantic wire type is
    the router's business (Task 4) and this module stays import-free of it.
    Members are read-only so any dataclass or ``BaseModel`` carrying these
    three attributes satisfies it.
    """

    @property
    def url(self) -> str:
        """Vendor-resolved download URL."""

    @property
    def filename(self) -> str:
        """Basename to land the bytes under, inside the LoRA directory."""

    @property
    def headers(self) -> Mapping[str, str]:
        """Vendor auth headers, applied verbatim."""


# Module-level because the pod serves one pipeline per process and the router
# reads this to answer /lora/inventory. A LIST, not a dict: a stack may hold two
# entries with the same ref AND the same resolved target (two adapters, two
# rows), which any keying would silently collapse into one row — an under-report
# from the one module whose job is inventory truthfulness. Nothing looks a row
# up by key, so a key buys nothing. One row per loaded adapter, in stack order.
_INVENTORY: list[InventoryEntry] = []


def inventory_snapshot() -> list[InventoryEntry]:
    """Return the currently applied stack, in activation order.

    Returns:
        A new list of the live rows — same shape ``apply_stack`` returns, so
        the router has exactly one inventory shape to serialize. One row per
        loaded adapter. Empty whenever no stack is applied, including after a
        rolled-back failure.
    """
    return list(_INVENTORY)


def disk_free_bytes(path: Path) -> int:
    """Return free bytes on the filesystem containing ``path``.

    Args:
        path: Any existing path on the filesystem of interest.

    Returns:
        Free space in bytes.
    """
    return shutil.disk_usage(path).free


def _resolve_target(entry: ResolvedEntry, profile: LoraProfile) -> str:
    """Return the target an entry loads into.

    Args:
        entry: The stack entry, whose ``target`` may be ``None``.
        profile: The model's profile, supplying the default.

    Returns:
        The resolved target name.

    Raises:
        TargetRequired: The entry names no target and the profile has no
            default. Guessing is not an option: on a multi-partition model the
            wrong partition loads without error and merely degrades the output.
    """
    target = entry.target or profile.default_target
    if target is None:
        raise TargetRequired(
            f"entry {entry.ref} declares no target and profile {profile.name} "
            f"has no default; legal targets: {list(profile.targets)}"
        )
    return target


def apply_stack(
    pipe: Any,  # noqa: ANN401 — a diffusers pipeline; the class differs per model and has no shared base.
    profile: LoraProfile,
    *,
    entries: Sequence[ResolvedEntry],
) -> list[InventoryEntry]:
    """Replace the pod's live LoRA stack with ``entries``, or change nothing.

    Sequence, in order: unload whatever is loaded; load each entry under the
    positional adapter name ``lora_{i}``; run the profile's whole-model
    ``after_load`` fixup once; call ``set_adapters`` once per distinct target,
    on the module the profile names, with that group's names and weights in
    stack order (peft raises on an adapter name the module does not own, which
    is why it is grouped rather than called once pipe-wide); then publish the
    new inventory.

    Failure path — the reason this function exists:

    * Every target is resolved BEFORE anything is unloaded, so a
      :class:`TargetRequired` entry anywhere in the stack leaves the pod's
      current stack and inventory untouched.
    * EVERY other failure — a load, the ``after_load`` fixup, a ``module_for``
      lookup, a ``set_adapters``, a vanished file at inventory time — unloads
      whatever landed and propagates, leaving the pod genuinely LoRA-free with
      an empty inventory. A load failure propagates as :class:`LoraLoadError`;
      anything else propagates unchanged.
    * The inventory is published in a single assignment, after the last row is
      built. It is empty or it is the whole stack; never a prefix.

    Args:
        pipe: The loaded diffusers pipeline.
        profile: Per-model behaviour supplied by that model's server.
        entries: The stack to apply, in activation order. Empty clears the pod.

    Returns:
        The new inventory rows, in activation order.

    Raises:
        TargetRequired: An entry names no target and the profile has no default.
        LoraLoadError: A load call failed; carries the ref and the profile's hint.
    """
    resolved = [(entry, _resolve_target(entry, profile)) for entry in entries]

    pipe.unload_lora_weights()
    # The stack the inventory described no longer exists, so the inventory must
    # not outlive it.
    _INVENTORY.clear()
    # ONE rollback boundary around everything that can leave adapters attached.
    # load_lora_weights attaches an adapter that is ACTIVE from that moment on:
    # an escape anywhere below — a raising after_load, a module_for lookup, a
    # set_adapters that fails on the second target after the first took, a stat
    # of a file evicted from under us — would otherwise leave the pod generating
    # WITH LoRAs (at default weight 1.0 if set_adapters never ran) while the
    # inventory reports none. Same real-vs-reported lie as a half-applied stack,
    # merely inverted, and equally invisible outside the pixels.
    try:
        for i, (entry, target) in enumerate(resolved):
            try:
                profile.load(pipe, str(entry.path), f"lora_{i}", target)
            except BaseException as exc:
                raise LoraLoadError(
                    entry.ref, profile.explain_load_failure(exc), exc
                ) from exc

        profile.after_load(pipe)

        by_target: dict[str, tuple[list[str], list[float]]] = {}
        for i, (entry, target) in enumerate(resolved):
            names, weights = by_target.setdefault(target, ([], []))
            names.append(f"lora_{i}")
            weights.append(entry.strength)
        for target, (names, weights) in by_target.items():
            profile.module_for(pipe, target).set_adapters(names, weights)

        # Built locally, published in one assignment below: a row that cannot be
        # built (the stat, typically) must not leave rows 0..k-1 on show against
        # a pipeline holding the whole weighted stack.
        rows = [
            InventoryEntry(
                ref=entry.ref,
                filename=entry.path.name,
                size_bytes=entry.path.stat().st_size,
                adapter_name=f"lora_{i}",
                strength=entry.strength,
                target=target,
            )
            for i, (entry, target) in enumerate(resolved)
        ]
    except BaseException:
        pipe.unload_lora_weights()
        raise

    _INVENTORY[:] = rows
    return inventory_snapshot()


def download_one(spec: DownloadSpec, dest_dir: Path) -> tuple[Path, int]:
    """Download one LoRA spec to dest_dir.

    Streams to a temp ``.partial`` file and renames on success so partial
    downloads never present as complete LoRA files. Raises on any HTTP / IO
    error after cleaning up the partial.

    Ported from ``wan_t2v_server._download_one``. The duplication is TEMPORARY
    and deliberate: the Wan server keeps its own copy until it migrates onto
    this seam, because changing Wan's download path is a separate, separately
    verified risk. Keep the two in step until that migration deletes one.

    Args:
        spec: Vendor-resolved download instruction.
        dest_dir: Directory to land the file in (created if missing).

    Returns:
        Tuple of (path on disk, actual bytes written).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / spec.filename
    tmp = dest_dir / f"{spec.filename}.partial"
    # Civitai (and other Cloudflare-fronted vendors) 403 the default
    # Python-urllib UA. Inject kinoforge-pod-download UA unless the
    # caller's spec.headers already pins one explicitly.
    download_headers = {"User-Agent": "kinoforge-pod-download/0.1", **spec.headers}
    req = urllib.request.Request(spec.url, headers=download_headers)  # noqa: S310 — vendor-resolved URL
    bytes_written = 0
    # 600s timeout guards against indefinite hangs on vendor stalls;
    # urlopen's default is socket._GLOBAL_DEFAULT_TIMEOUT (None) which
    # blocks forever and burns the smoke's wall-clock + budget.
    try:
        with urllib.request.urlopen(req, timeout=600) as resp, tmp.open("wb") as out:  # noqa: S310
            while True:
                chunk = resp.read(64 * 1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                bytes_written += len(chunk)
        tmp.replace(target)
        return target, bytes_written
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def _ref_dirname(ref: str) -> str:
    """Return the per-ref subdirectory name that holds that ref's bytes.

    Vendor filenames are NOT unique — ``pytorch_lora_weights.safetensors`` is a
    common CivitAI/HF basename — so a flat directory plus reuse-by-basename
    would serve one ref's bytes under another ref's name, silently and forever
    on a warm pod. The ref is the only identity available here (``DownloadSpec``
    carries no digest or size), so it becomes the directory.

    Args:
        ref: Controller-side LoRA reference, e.g. ``civitai:1234@5678``.

    Returns:
        A single path segment: every character outside ``[A-Za-z0-9._-]``
        replaced, so no separator and no ``..`` can survive, plus a short digest
        of the RAW ref so two refs cannot collide by sanitizing to the same text.
    """
    safe = _UNSAFE_REF_CHARS.sub("_", ref).lstrip(".")[:80] or "ref"
    return f"{safe}-{hashlib.sha256(ref.encode()).hexdigest()[:8]}"


def ensure_downloaded(
    ref: str, spec: DownloadSpec, loras_dir: Path
) -> tuple[Path, int]:
    """Return the file's path, downloading it only if this REF already has it.

    Warm-reuse pods call this on every stack change, so re-fetching bytes that
    are already on disk is a per-call bandwidth and wall-clock tax. The reuse
    check is per ref, not per filename — see :func:`_ref_dirname`.

    Args:
        ref: Controller-side LoRA reference. Namespaces the bytes on disk, and
            names the cause on failure.
        spec: Vendor-resolved download instruction.
        loras_dir: Root directory LoRA files live under. This ref's bytes land
            in a subdirectory of it.

    Returns:
        Tuple of (path on disk, size in bytes).

    Raises:
        RuntimeError: The download failed; the underlying cause is chained.
    """
    dest_dir = loras_dir / _ref_dirname(ref)
    existing = dest_dir / spec.filename
    if existing.exists():
        return existing, existing.stat().st_size
    try:
        return download_one(spec, dest_dir)
    except Exception as exc:
        raise RuntimeError(
            f"downloading LoRA {ref} ({spec.filename}) failed: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Wire contract
# ---------------------------------------------------------------------------
#
# Field names below are NOT free to change: `DiffusersBackend.set_lora_stack`
# and `_poll_set_stack` are ONE client shared by the Wan pod and this one, so a
# rename here silently breaks whichever pod answers. The shapes are copied from
# `wan_t2v_server` (`ArtifactDownloadSpec`, `LoraTarget`, `SetStackRequest`,
# `LoraInventoryEntry`, `InventoryResponse`) rather than imported, because the
# pod cannot import `kinoforge.*` and the Wan server is not importable here
# either — it pulls torch at module scope.


class ArtifactDownloadSpec(BaseModel):
    """Pre-resolved LoRA download instruction sent by the orchestrator.

    The orchestrator resolves vendor-specific download URLs + headers (CivitAI
    bearer tokens, HF auth) on its side and ships an opaque spec to the pod, so
    no vendor credential and no vendor-specific code path ever reaches here.
    """

    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    filename: str
    size_hint: int | None = None


class LoraTarget(BaseModel):
    """One entry in the ``/lora/set_stack`` target list.

    The THIRD copy of this schema — ``kinoforge.core.lora.LoraEntry`` is the
    config-side one and ``wan_t2v_server.LoraTarget`` the Wan pod's. Copies,
    not imports, because a pod has no ``kinoforge.core``. Three copies is how
    schemas drift, so ``tests/test_lora_schema_parity.py`` runs the same
    assertions over every server copy against core. DO NOT diverge.

    ``branch`` is Wan's MoE vocabulary and is DEPRECATED in favour of
    ``target``, which can also name a workflow partition (H3's ``transformer``
    / ``transformer_ref``) that ``branch``'s Literal cannot express. The two
    may not disagree; ``branch`` is mapped onto ``target`` when only it is set.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    strength: float = Field(default=1.0, ge=-2.0, le=2.0)
    branch: Literal["high_noise", "low_noise", "auto"] = Field(default="auto")
    target: str | None = Field(default=None)

    @field_validator("branch", mode="before")
    @classmethod
    def _normalize_branch_alias(cls, v: Any) -> Any:  # noqa: ANN401 — arbitrary wire input.
        """Mirror of ``LoraEntry._normalize_branch_alias`` in core/lora.py.

        Parity is load-bearing — ``tests/test_lora_schema_parity.py`` asserts
        every copy normalizes identically. DO NOT diverge.
        """
        if v == "h":
            return "high_noise"
        if v == "l":
            return "low_noise"
        return v

    @model_validator(mode="after")
    def _resolve_branch_to_target(self) -> LoraTarget:
        """Mirror of ``LoraEntry._resolve_branch_to_target`` in core/lora.py.

        Parity is load-bearing — ``tests/test_lora_schema_parity.py`` asserts
        every copy resolves ``branch`` / ``target`` identically. DO NOT
        diverge.
        """
        implied = None if self.branch == "auto" else self.branch
        if implied is not None and self.target is not None and implied != self.target:
            raise ValueError(
                f"branch and target disagree: branch={self.branch!r} implies "
                f"target={implied!r}, but target={self.target!r} was set; "
                f"set only `target` (branch is deprecated)"
            )
        if implied is not None and self.target is None:
            object.__setattr__(self, "target", implied)
            # Deliberately ref-free: this line lands in the pod log, and a LoRA
            # ref is prompt-laden under vault mode.
            _log.warning(
                "deprecated-lora-branch: 1 entry used `branch`; it is mapped to "
                "`target`. Use `target:` — see docs/breaking-changes.md"
            )
        return self


class SetStackRequest(BaseModel):
    """Declarative target LoRA stack for the pod.

    The order of ``target`` is the activation order. Every ref in ``target``
    needs an entry in ``download_specs``; the pod reuses bytes it already holds
    for that ref, so a warm pod pays nothing for a spec it has already fetched.
    """

    model_config = ConfigDict(extra="forbid")

    target: list[LoraTarget]
    download_specs: dict[str, ArtifactDownloadSpec]


class LoraInventoryEntryModel(BaseModel):
    """One row of the pod's LoRA inventory on the wire.

    Names match ``wan_t2v_server.LoraInventoryEntry`` field-for-field for every
    value this seam actually has: ``last_strength`` is the per-adapter
    ``set_adapters`` weight and ``branch`` carries the resolved routing token
    (Wan types it ``str``, not a Literal, so ``"transformer"`` is legal in it).
    ``target`` is the same value under the name that supersedes ``branch``.

    Wan's two LRU clocks — ``downloaded_at_local`` / ``last_used_at_local`` —
    are deliberately absent: this seam has no eviction, so there is no LRU and
    nothing truthful to put in them. Every shared consumer reads them with a
    default (``e.get("last_used_at_local", "?")``), so the absence renders, it
    does not break.
    """

    ref: str
    filename: str
    size_bytes: int
    adapter_name: str
    last_strength: float | None = None
    branch: str = "auto"
    target: str | None = None


class InventoryResponse(BaseModel):
    """Read-only snapshot of the pod's LoRA inventory + free disk bytes."""

    inventory: list[LoraInventoryEntryModel]
    free_bytes: int


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
#
# Module-level, not per-router: a pod serves ONE pipeline per process, so one
# lock serialises every stack change against every inventory read, and one job
# table answers every status poll. Build two routers in one process and they
# correctly contend on the same pipeline.
_SET_STACK_LOCK: asyncio.Lock = asyncio.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
# asyncio keeps only a weak reference to a bare create_task result, so a job
# can be garbage-collected mid-download; hold a strong one until it finishes.
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


def _inventory_rows() -> list[LoraInventoryEntryModel]:
    """Return the live inventory in wire shape, in activation order.

    Returns:
        One row per loaded adapter — the same rows :func:`inventory_snapshot`
        reports, so ``/lora/inventory`` and a ``done`` job record can never
        disagree about what is loaded.
    """
    return [
        LoraInventoryEntryModel(
            ref=row.ref,
            filename=row.filename,
            size_bytes=row.size_bytes,
            adapter_name=row.adapter_name,
            last_strength=row.strength,
            # U55. NOT `row.target`. `branch` holds Wan's MoE vocabulary
            # (`high_noise` / `low_noise` / `auto`), which cannot name an H3
            # workflow partition — stamping `"transformer"` here put a token
            # in a field whose documented vocabulary excludes it, and the
            # warm-reuse matcher compared it against a cfg-side `.branch` that
            # stays `"auto"` for any cfg using `target:`. `"auto"` is the
            # honest Wan-vocabulary value for a non-MoE pipeline; the real
            # routing token rides in `target`, which is what the matcher reads.
            branch="auto",
            target=row.target,
        )
        for row in inventory_snapshot()
    ]


def _check_target_legal(entry: LoraTarget, profile: LoraProfile) -> None:
    """Refuse an entry this pipeline cannot route, before anything downloads.

    Runs on the SYNCHRONOUS submit path. A legality gate inside the job spends
    a 1.4 GB download first and then fails for a reason that was knowable at
    the POST.

    Args:
        entry: One requested stack entry.
        profile: The loaded pipeline's profile, owning the legal vocabulary.

    Raises:
        HTTPException: 400 ``lora_target_unsupported``, listing what is legal.
            Raised both for a named target this pipeline does not hold and for
            an unnamed one on a pipeline that has no default to fall back on
            (``target: null`` in the body) — guessing a partition loads the
            wrong one without erroring and merely degrades the output.
    """
    named_but_absent = entry.target is not None and entry.target not in profile.targets
    unnamed_and_undecidable = entry.target is None and profile.default_target is None
    if named_but_absent or unnamed_and_undecidable:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "lora_target_unsupported",
                "target": entry.target,
                "legal": list(profile.targets),
            },
        )


def _load_error_to_http(exc: LoraLoadError) -> HTTPException:
    """Turn a failed load into the status the controller can act on.

    A hint means the profile recognised the cause — a LoRA trained against a
    pruned checkpoint, typically — which no retry can fix, so it is a 400. No
    hint means an unexplained load failure, which is a 500.

    Args:
        exc: The load failure, carrying the ref and the profile's hint.

    Returns:
        The HTTPException whose detail becomes the job's ``error`` body.
    """
    if exc.hint:
        return HTTPException(
            status_code=400,
            detail={
                "error": "lora_format_unsupported",
                "hint": exc.hint,
                "ref": exc.ref,
                "underlying": str(exc.underlying),
            },
        )
    return HTTPException(
        status_code=500,
        detail={
            "error": "lora_load_failed",
            "ref": exc.ref,
            "underlying": str(exc.underlying),
        },
    )


def _download_failed(
    ref: str, download_completed: list[str], underlying: str
) -> HTTPException:
    """Build the 502 body for a ref whose bytes never landed.

    ``evict_completed`` is always empty — this seam never evicts — and that is
    load-bearing, not decorative: the shared client reads a non-empty
    ``evict_completed`` as "the pod is in a half-state" and raises
    ``LoraSwapDegradedPodError`` instead of the retryable download error.

    Args:
        ref: The ref that failed.
        download_completed: Refs whose bytes did land, in download order.
        underlying: Operator-readable cause.

    Returns:
        The HTTPException whose detail becomes the job's ``error`` body.
    """
    return HTTPException(
        status_code=502,
        detail={
            "error": "lora_download_failed",
            "phase": "download",
            "evict_completed": [],
            "download_completed": list(download_completed),
            "download_failed": ref,
            "underlying": underlying,
        },
    )


async def _download_stack(req: SetStackRequest, loras_dir: Path) -> dict[str, Path]:
    """Land every requested ref's bytes on disk, reusing what is already there.

    Each download runs via ``asyncio.to_thread``: ``ensure_downloaded`` is sync
    urllib plus blocking file IO, and running a multi-hundred-MB fetch inline
    blocks the event loop, which hangs ``/health``, which makes the provider
    proxy answer 502 while uvicorn is perfectly alive.

    Args:
        req: The declarative target stack + per-ref download specs.
        loras_dir: Root directory LoRA bytes live under.

    Returns:
        Map of ref -> path on disk, one entry per DISTINCT ref.

    Raises:
        HTTPException: 502 ``lora_download_failed`` naming the ref that failed.
            ``evict_completed`` is always empty — this seam never evicts — which
            is what tells the client the pod's stack is untouched and the call
            is safe to retry.
    """
    paths: dict[str, Path] = {}
    download_completed: list[str] = []
    for entry in req.target:
        if entry.ref in paths:
            continue
        spec = req.download_specs.get(entry.ref)
        if spec is None:
            # A ref with no spec is a client bug, but it surfaces as a download
            # failure so it still reaches the controller as a TYPED error
            # naming the ref, rather than as an unmapped body it can only
            # stringify.
            raise _download_failed(
                entry.ref, download_completed, "no download spec supplied for this ref"
            )
        try:
            path, _size = await asyncio.to_thread(
                ensure_downloaded, entry.ref, spec, loras_dir
            )
        except Exception as exc:
            # Log before raising: the raised detail only travels in the job
            # record, and the bootstrap sidecar log is where a live smoke looks.
            _log.warning("set_stack download failed for ref=%s: %r", entry.ref, exc)
            raise _download_failed(entry.ref, download_completed, str(exc)) from exc
        paths[entry.ref] = path
        download_completed.append(entry.ref)
    return paths


def _record_done(job_id: str, loras_dir: Path) -> None:
    """Write the terminal ``done`` record for an apply job.

    The result fields are written BEFORE ``state`` so a status poll can never
    observe a ``done`` job without its result.

    Args:
        job_id: Key into the job table.
        loras_dir: Root LoRA directory, for the free-disk snapshot.
    """
    _JOBS[job_id]["inventory"] = [row.model_dump() for row in _inventory_rows()]
    _JOBS[job_id]["free_bytes"] = disk_free_bytes(loras_dir)
    # No VRAM-OOM rollback on this seam, but the key is part of the contract:
    # the shared client reads `swap_rejected` on every done record.
    _JOBS[job_id]["swap_rejected"] = None
    _JOBS[job_id]["state"] = "done"


def _record_error(job_id: str, status: int, detail: dict[str, Any]) -> None:
    """Write the terminal ``error`` record, ``error`` body before ``state``.

    Args:
        job_id: Key into the job table.
        status: HTTP status the controller should map this failure to.
        detail: The structured error body; ``status`` is merged into it.
    """
    _JOBS[job_id]["error"] = {**detail, "status": status}
    _JOBS[job_id]["state"] = "error"


async def _run_apply_job(
    job_id: str,
    req: SetStackRequest,
    get_pipe: Callable[[], Any],
    get_profile: Callable[[], LoraProfile],
    loras_dir: Path,
) -> None:
    """Download the stack, apply it, and write the job's terminal record.

    Target legality was already settled on the submit path, so everything that
    can fail here is a download, a load, or the rollback behind a load.

    NOTHING may escape this coroutine: an unhandled exception leaves the job
    stuck in ``running`` with no ``error`` body, and the controller learns that
    only when its wall-clock poll budget expires — minutes of a booked GPU
    spent discovering a failure that happened immediately. That includes the
    secondary exception from a rollback: ``apply_stack`` calls
    ``unload_lora_weights`` on its way out, and if THAT raises it replaces the
    ``LoraLoadError`` on the way up, so the generic handler below is the one
    that catches it.

    Args:
        job_id: Key into the job table.
        req: The declarative target stack + per-ref download specs.
        get_pipe: Accessor for the loaded pipeline.
        get_profile: Accessor for the model's LoRA profile.
        loras_dir: Root directory LoRA bytes live under.
    """
    _JOBS[job_id]["state"] = "running"
    try:
        async with _SET_STACK_LOCK:
            paths = await _download_stack(req, loras_dir)
            entries = [
                ResolvedEntry(
                    ref=entry.ref,
                    path=paths[entry.ref],
                    strength=entry.strength,
                    target=entry.target,
                )
                for entry in req.target
            ]
            try:
                # to_thread: the load is synchronous, slow, and CUDA-bound.
                # Inline it and the event loop stops answering /health for the
                # duration, which the provider proxy reports as a 502.
                await asyncio.to_thread(
                    apply_stack, get_pipe(), get_profile(), entries=entries
                )
            except LoraLoadError as exc:
                raise _load_error_to_http(exc) from exc
            _record_done(job_id, loras_dir)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}
        _record_error(job_id, exc.status_code, detail)
    except Exception as exc:  # noqa: BLE001 — terminal record beats an escaped crash.
        _log.exception("lora apply job %s failed", job_id)
        _record_error(
            job_id, 500, {"error": "lora_swap_failed", "underlying": str(exc)}
        )


def build_lora_router(
    get_pipe: Callable[[], Any],
    get_profile: Callable[[], LoraProfile],
    loras_dir: Path,
) -> APIRouter:
    """Build the three LoRA endpoints for a pod server to mount.

    Accessors rather than objects: a server includes this router during startup
    and the pipeline may not exist until that startup finishes, and the later
    Wan migration must not care how its server holds its pipeline.

    Endpoints, matching what ``DiffusersBackend`` already speaks:

    * ``GET /lora/inventory`` -> ``{inventory, free_bytes}``, read under the
      swap lock so a snapshot cannot catch a stack mid-change.
    * ``POST /lora/set_stack`` -> ``{"job_id": ...}``. Async is not optional: a
      ~1.4 GB download through a provider proxy is why this contract was made
      a job in the first place.
    * ``GET /lora/set_stack/status/{job_id}`` -> the job record; 404 when
      unknown.

    Args:
        get_pipe: Accessor for the loaded pipeline.
        get_profile: Accessor for the model's LoRA profile.
        loras_dir: Root directory LoRA bytes live under.

    Returns:
        The router, ready for ``app.include_router``.
    """
    router = APIRouter()

    @router.get("/lora/inventory")
    async def inventory() -> InventoryResponse:
        """Return the pod's current LoRA inventory + free disk, under the lock."""
        async with _SET_STACK_LOCK:
            return InventoryResponse(
                inventory=_inventory_rows(),
                free_bytes=disk_free_bytes(loras_dir),
            )

    @router.post("/lora/set_stack")
    async def set_stack(req: SetStackRequest) -> dict[str, str]:
        """Validate synchronously, enqueue the apply job, return its id.

        Args:
            req: Declarative target stack + per-ref download specs.

        Returns:
            ``{"job_id": ...}``; poll the status endpoint for the result.

        Raises:
            HTTPException: 400 ``lora_target_unsupported`` — refused here, on
                the submit path, so no byte is downloaded for a stack that
                could never have been routed.
        """
        profile = get_profile()
        for entry in req.target:
            _check_target_legal(entry, profile)

        job_id = f"s-{uuid.uuid4().hex}"
        _JOBS[job_id] = {
            "state": "queued",
            "inventory": None,
            "free_bytes": None,
            "swap_rejected": None,
            "error": None,
        }
        task = asyncio.create_task(
            _run_apply_job(job_id, req, get_pipe, get_profile, loras_dir)
        )
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
        return {"job_id": job_id}

    @router.get("/lora/set_stack/status/{job_id}")
    def set_stack_status(job_id: str) -> dict[str, Any]:
        """Return the apply job's record.

        States: ``queued`` -> ``running`` -> ``done`` | ``error``. A ``done``
        record carries ``inventory``, ``free_bytes`` and ``swap_rejected``; an
        ``error`` record carries an ``error`` body whose ``status`` is the code
        the controller maps.

        Args:
            job_id: The id returned by the submit POST.

        Returns:
            The job record.

        Raises:
            HTTPException: 404 when no such job exists.
        """
        payload = _JOBS.get(job_id)
        if payload is None:
            raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
        return payload

    return router
