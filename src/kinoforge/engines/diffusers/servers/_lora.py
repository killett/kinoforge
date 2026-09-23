"""Shared LoRA seam for the diffusers pod servers.

Runs INSIDE the pod, where ``kinoforge.core`` does NOT exist — the whole
``servers/`` package is base64-embedded into the boot script, so this module
imports stdlib only, exactly like its siblings ``_av_io`` and ``_util_stats``.

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
"""

from __future__ import annotations

import shutil
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


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
# reads this to answer /lora/inventory. Keyed by (ref, target): the same ref may
# legitimately be loaded into two different partitions of the same model.
_INVENTORY: dict[tuple[str, str], InventoryEntry] = {}


def inventory_snapshot() -> list[InventoryEntry]:
    """Return the currently applied stack, in activation order.

    Returns:
        A new list of the live rows — same shape ``apply_stack`` returns, so
        the router has exactly one inventory shape to serialize. Empty whenever
        no stack is applied, including after a rolled-back failure.
    """
    return list(_INVENTORY.values())


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
    * A load that raises unloads everything that already landed, empties the
      inventory, and re-raises as :class:`LoraLoadError`. The pod is then
      LoRA-free and honestly reports so; it never serves a partial stack under
      a full inventory.

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
    # not outlive it: emptying here (rather than only on the failure path) means
    # EVERY exit below this point short of success reports an empty stack.
    _INVENTORY.clear()
    for i, (entry, target) in enumerate(resolved):
        try:
            profile.load(pipe, str(entry.path), f"lora_{i}", target)
        except BaseException as exc:
            pipe.unload_lora_weights()
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

    # Published last: the inventory only ever describes a stack that fully landed.
    for i, (entry, target) in enumerate(resolved):
        _INVENTORY[(entry.ref, target)] = InventoryEntry(
            ref=entry.ref,
            filename=entry.path.name,
            size_bytes=entry.path.stat().st_size,
            adapter_name=f"lora_{i}",
            strength=entry.strength,
            target=target,
        )
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


def ensure_downloaded(
    ref: str, spec: DownloadSpec, loras_dir: Path
) -> tuple[Path, int]:
    """Return the file's path, downloading it only if it is not already there.

    Warm-reuse pods call this on every stack change, so re-fetching bytes that
    are already on disk is a per-call bandwidth and wall-clock tax.

    Args:
        ref: Controller-side LoRA reference, used to name the cause on failure.
        spec: Vendor-resolved download instruction.
        loras_dir: Directory LoRA files live in.

    Returns:
        Tuple of (path on disk, size in bytes).

    Raises:
        RuntimeError: The download failed; the underlying cause is chained.
    """
    existing = loras_dir / spec.filename
    if existing.exists():
        return existing, existing.stat().st_size
    try:
        return download_one(spec, loras_dir)
    except Exception as exc:
        raise RuntimeError(
            f"downloading LoRA {ref} ({spec.filename}) failed: {exc}"
        ) from exc
