"""Apply the resolved LoRA stack to a ready pod, before the first job.

This module closes the defect the H3 LoRA shared seam exists to remove:
a ``loras:`` block aimed at a pod that never loaded it used to produce a
plausible-looking video with no LoRA in it, and nothing anywhere said
so. The rule here is absolute — **a stack that did not load costs an
error, never a silent generation.** Every failure raised by the backend
propagates untouched; nothing is downgraded to a warning.

Download specs are resolved CONTROLLER-SIDE: the controller asks each
``ModelSource`` for the artifact and ships the pod a plain
``{url, headers, filename, size_hint}`` dict.

Be precise about what that buys, because it is easy to overclaim. The
``headers`` a source mints for ``hf:`` and ``civitai:`` refs carry a
**bearer token** (``sources/huggingface/__init__.py`` and
``sources/civitai/__init__.py`` both build ``Authorization: Bearer
<token>``), and that header is copied verbatim into the spec below —
so the token DOES travel to the pod, inside the ``set_stack`` request
body, because the pod has to replay it on the download. What the pod
never gets is a *stored* vendor credential: no ``HF_TOKEN`` /
``CIVITAI_TOKEN`` in its env, no credential file, nothing that outlives
the request or lets it fetch anything the controller did not resolve
for it. Treat that as the guarantee; "the token never reaches the pod"
is not true and must not be relied on.

Privacy
-------
A LoRA ``ref`` is SENSITIVE under vault mode (ephemeral spec D4). Every
log line in this module therefore reports **counts, never refs** — the
same posture ``core.lora.resolve_active_lora_stack`` already takes when
it logs "vault.loras (%d entries) bypassed". The one exception this
module raises is ref-free for the same reason; failures raised by the
pod client name the ref by design (it is the operator's recovery hint
at the CLI boundary) and are re-raised untouched rather than re-logged
here.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from kinoforge.core import registry
from kinoforge.core.errors import ValidationError
from kinoforge.core.logging import get_logger
from kinoforge.core.lora_capability import LoraSupport
from kinoforge.core.warm_reuse.integration import _entry_to_dict
from kinoforge.core.warm_reuse.redaction import _register_observed_lora_refs

if TYPE_CHECKING:
    from kinoforge.core.interfaces import CredentialProvider
    from kinoforge.core.lora import LoraEntry

_log = get_logger("core.lora_apply")


@runtime_checkable
class SupportsSetLoraStack(Protocol):
    """A backend that can be told which LoRAs to hold.

    Structural, not nominal, on purpose: the check that matters is
    "does this backend expose the set-stack surface", and hosted
    engines answer no simply by not having the method. Declaring the
    shape here lets :func:`ensure_lora_stack` narrow ``object`` without
    an ``Any`` escape hatch.
    """

    def set_lora_stack(
        self,
        *,
        pod_id: str,
        active_stack: list[LoraEntry],
        download_specs: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Replace the pod's active LoRA stack. See ``DiffusersBackend``."""
        ...  # pragma: no cover — Protocol body


def resolve_download_specs(
    refs: list[str], creds: CredentialProvider
) -> dict[str, dict[str, Any]]:
    """Resolve each ref to the pod-side download spec shape.

    Routes through :func:`kinoforge.core.registry.source_for_ref` so
    ``hf:``, ``civitai:``, ``civarchive:`` and ``http:`` all work with
    no vendor code here. A source that returns several artifacts (a
    CivitAI model version ships previews and companion files alongside
    the weights) is narrowed to its ``.safetensors`` member; packs that
    ship no ``.safetensors`` at all fall back to the first artifact.

    A repeated ref costs one resolve, not two — the same adapter listed
    at two strengths must not double the vendor API calls.

    Args:
        refs: Ordered LoRA refs from the resolved stack. Duplicates are
            allowed and collapse to one entry.
        creds: Credential provider handed to each source's ``resolve``;
            this is where ``HF_TOKEN`` / ``CIVITAI_TOKEN`` are read, on
            the controller, so they never reach a pod.

    Returns:
        Map of ref to ``{url, headers, filename, size_hint}``. The map
        covers every distinct ref in ``refs``.

    Raises:
        ValidationError: A ref resolved to zero artifacts. The message
            carries NO ref text — it names the stack position instead,
            because the ref may be a vault secret.
        UnknownAdapter: No registered source handles a ref (raised by
            :func:`~kinoforge.core.registry.source_for_ref`).
    """
    specs: dict[str, dict[str, Any]] = {}
    for position, ref in enumerate(refs):
        if ref in specs:
            continue
        source = registry.source_for_ref(ref)
        artifacts = source.resolve(ref, creds)
        if not artifacts:
            # Privacy: position, never the ref. An omitted spec would let
            # the pod be asked to activate a LoRA it was never told how
            # to fetch, so this is an error rather than a skip.
            raise ValidationError(
                f"LoRA stack entry #{position + 1} resolved to zero "
                f"downloadable artifacts (source scheme "
                f"{source.scheme!r}); the pod cannot fetch it"
            )
        pick = next(
            (a for a in artifacts if a.filename.endswith(".safetensors")),
            artifacts[0],
        )
        specs[ref] = {
            "url": pick.url,
            "headers": dict(pick.headers or {}),
            "filename": pick.filename,
            "size_hint": pick.size,
        }
    return specs


def _engine_lora_support(cfg: Any) -> LoraSupport:  # noqa: ANN401 — a Config
    """Return how this cfg's engine applies a LoRA stack (U56).

    Degrades to ``SERVER_HTTP`` — the noisy answer — when the engine cannot be
    resolved or consulted, matching the ABC's own default. That direction is
    deliberate: not knowing the stack was handled elsewhere is not evidence
    that it was, and staying quiet about an unapplied stack is the exact
    failure U56 is about.

    Args:
        cfg: The loaded configuration.

    Returns:
        The engine's declared :class:`LoraSupport`.
    """
    try:
        return registry.get_engine(_engine_kind(cfg))().lora_support()
    except Exception:  # noqa: BLE001 — a bookkeeping read must not fail a run
        return LoraSupport.SERVER_HTTP


def ensure_lora_stack(
    *,
    backend: object,
    cfg: Any,  # noqa: ANN401 — a Config; typed loosely to avoid an import cycle
    pod_id: str | None,
    creds: CredentialProvider | None,
    ledger: Any,  # noqa: ANN401 — duck-typed Ledger, as warm_reuse/integration.py
) -> None:
    """Apply cfg/vault/CLI's resolved LoRA stack to a ready pod.

    Called from ``orchestrator.deploy_session`` once the pod reports
    ready and before any job is submitted, so cold pods and
    caller-supplied warm pods take the identical path.

    No-ops when no stack was asked about at all, when the backend has no
    ``set_lora_stack`` (hosted engines), or when there is no pod to
    address. Any failure propagates: a stack that did not load must
    fail the run rather than silently produce a LoRA-less video.

    An **explicitly empty CLI stack** (``--loras ""``) is not an absence
    — it is a request to clear — and against a pod it issues a clearing
    ``set_lora_stack`` with an empty stack and no download specs. See
    the ``explicit_clear`` comment in the body for why the two cases
    cannot be collapsed.

    Args:
        backend: The generation backend for this session. Duck-typed —
            a backend without ``set_lora_stack`` is a no-op.
        cfg: The loaded kinoforge configuration.
        pod_id: Identifier of the ready pod, or ``None`` on a hosted
            path where there is no pod.
        creds: Credential provider used to resolve download specs.
            ``None`` falls back to the environment, matching
            ``deploy_session``'s own optional-creds contract.
        ledger: Duck-typed :class:`~kinoforge.core.lifecycle.Ledger` the
            pod's post-apply inventory is recorded into, so ``kinoforge
            list`` / ``inspect`` and the warm-reuse matcher see what
            this pod actually holds. ``None`` skips the write. Required
            keyword (not defaulted) so a new call site has to decide
            rather than silently inherit a no-op.

    Raises:
        LoraStackConflict: ``cfg.loras`` and ``vault.loras`` disagree
            (raised by ``resolve_active_lora_stack``).
        LoraSwapError: Any pod-side refusal — format unsupported, load
            failed, download failed, disk full, VRAM OOM, unreachable.
            Re-raised untouched; NEVER downgraded to a warning.
        ValidationError: A ref resolved to no downloadable artifact.
    """
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.ephemeral import EphemeralSession
    from kinoforge.core.lora import resolve_active_lora_stack

    # Same precedence source as the warm-reuse swap path
    # (core/warm_reuse/integration.py): CLI > vault > cfg, read off the
    # active EphemeralSession so --loras flows end-to-end without a
    # kwarg threaded through every orchestrator hop.
    _session = EphemeralSession.current()
    _vault = _session.vault if _session is not None else None
    _cli_loras = getattr(_session, "cli_loras", None) if _session else None
    stack = resolve_active_lora_stack(cfg, _vault, cli_loras=_cli_loras)

    # `--loras ""` is a REQUEST ("hold nothing"), not an absence ("nothing
    # was said about LoRAs"). `resolve_active_lora_stack` is the only place
    # that still keeps the two apart — `cli_loras is None` vs `[]`, see its
    # docstring — and the resolved stack is `[]` either way, so the
    # distinction has to be read off `_cli_loras` here or it is lost. It
    # matters because a warm pod carries the PREVIOUS run's adapters:
    # `kinoforge grid` emits `--loras ""` for a swap grid's control cell
    # (`core/grid/executor.py`, cells 2..N sharing one pod), and without
    # the clearing call that control renders with the previous cell's LoRA.
    explicit_clear = _cli_loras is not None and not stack

    if not stack and not explicit_clear:
        # Zero HTTP calls for runs without LoRAs: an unconditional POST
        # would 404 against every server build with no LoRA surface.
        return

    if not isinstance(backend, SupportsSetLoraStack):
        if explicit_clear:
            # Nothing to clear: this seam never loaded anything onto a
            # backend without the surface, so there is no stale stack for
            # `--loras ""` to be lying about. Deliberately NOT the discard
            # WARNING below — "NOT APPLYING 0 LoRA entries" would be a loud
            # alarm about a discrepancy that does not exist, and that is how
            # a load-bearing warning gets trained out of its reader.
            _log.info(
                "lora-apply: nothing to clear — engine %r's backend (%s) has "
                "no set_lora_stack surface, so no stack was ever loaded "
                "through this seam.",
                _engine_kind(cfg),
                type(backend).__name__,
            )
            return
        # This WARNING is the only protection this case gets, and it has
        # to earn its place in a multi-minute boot log.
        #
        # A cfg-load fatality cannot cover it: `--loras` never enters
        # `cfg.loras` (it lives on the EphemeralSession), so a static
        # check reading the cfg misses the CLI path entirely. A fatal
        # gate would also be wrong — a ComfyUI config legitimately
        # carries `loras:` to feed `capability_key()` while applying the
        # adapters through workflow NODES, never through this seam.
        #
        # So: say what is NOT happening, name the engine so the reader
        # can tell the two cases apart, and give the fix.
        #
        # U56 supplied the discriminator the two paragraphs above were
        # reaching for: the engine now DECLARES how it applies a stack, so
        # this seam can tell "should have applied, did not" from "was never
        # this seam's job". Both cases still say something; only the first
        # is an alarm.
        if _engine_lora_support(cfg) is LoraSupport.WORKFLOW:
            # Nothing is wrong. The engine applies adapters through its own
            # graph and this seam correctly did nothing. Warning here fires
            # on every healthy ComfyUI LoRA run, and a warning that fires on
            # a correct configuration is how a load-bearing alarm gets
            # trained out of its reader — the same reasoning that gave
            # `--loras ""` an INFO rather than the discard WARNING.
            _log.info(
                "lora-apply: %d LoRA entries left to engine %r, which applies "
                "them through its workflow graph rather than this seam.",
                len(stack),
                _engine_kind(cfg),
            )
            return
        # Now the WARNING means what it says: the engine declares it CAN
        # apply a stack, and the backend handed to us cannot.
        _log.warning(
            "lora-apply: NOT APPLYING %d LoRA entries — engine %r declares "
            "LoRA support but its backend (%s) has no set_lora_stack "
            "surface, so this run generates WITHOUT them. Drop the `loras:` "
            "block / `--loras` stack, or use a config whose "
            "engine.diffusers.server_cmd serves LoRAs (see "
            "kinoforge.core.lora_profiles).",
            len(stack),
            _engine_kind(cfg),
            type(backend).__name__,
        )
        return

    if pod_id is None:
        if explicit_clear:
            # Same reasoning as the branch above, different cause: the
            # surface exists but there is no pod holding anything to clear.
            _log.info(
                "lora-apply: nothing to clear — this session has no instance "
                "to address, so no pod holds a stack from a previous run."
            )
            return
        # A LoRA-capable backend with no pod behind it. Distinct from the
        # branch above — the surface exists, there is just nothing to
        # address — so the fix hint is different too.
        _log.warning(
            "lora-apply: NOT APPLYING %d LoRA entries — engine %r's backend "
            "(%s) can load LoRAs but this session has no instance to address "
            "(hosted path), so this run generates WITHOUT them. Run this "
            "config against a compute provider to apply the stack.",
            len(stack),
            _engine_kind(cfg),
            type(backend).__name__,
        )
        return

    if explicit_clear:
        _log.info("lora-apply: clearing pod %s's LoRA stack (--loras is empty)", pod_id)
    else:
        _log.info("lora-apply: applying %d LoRA entries to pod %s", len(stack), pod_id)
    # A clear resolves nothing: `stack` is empty, so this returns `{}`
    # without touching a vendor API or a credential.
    specs = resolve_download_specs(
        [lo.ref for lo in stack], creds or EnvCredentialProvider()
    )
    # Bare call, no try/except: every LoraSwapError subclass must reach
    # the operator. Swallowing one here is exactly the silent defect
    # this module was written to remove.
    resp = backend.set_lora_stack(
        pod_id=pod_id, active_stack=stack, download_specs=specs
    )
    if explicit_clear:
        _log.info("lora-apply: pod %s cleared its LoRA stack", pod_id)
    else:
        _log.info("lora-apply: pod %s accepted the %d-entry stack", pod_id, len(stack))
    # The emptied inventory is written back for the same reason a full one
    # is: a row saying the pod still holds adapters it just dropped is the
    # lie `kinoforge list` and the warm-reuse matcher both read.
    _record_inventory(ledger, pod_id, resp)


def _engine_kind(cfg: Any) -> str:  # noqa: ANN401 — duck-typed Config
    """Return ``cfg.engine.kind`` for a log line, or ``"?"``.

    Args:
        cfg: The loaded configuration.

    Returns:
        The engine kind, or ``"?"`` when the cfg has no readable one.
    """
    kind = getattr(getattr(cfg, "engine", None), "kind", None)
    return kind if isinstance(kind, str) else "?"


def _record_inventory(
    ledger: Any,  # noqa: ANN401 — duck-typed Ledger
    pod_id: str,
    resp: object,
) -> None:
    """Record the pod's post-apply LoRA inventory into its ledger row.

    Field for field the same write ``warm_reuse/integration.py`` makes
    after its swap, through the same two helpers, so the cold-apply path
    and the swap path leave rows the readers cannot tell apart. Without
    it ``kinoforge list`` / ``inspect`` render an empty LoRA section for
    a pod that just accepted a stack (the tool lying about pod state),
    and ``warm_reuse/matcher.py`` plans swaps against an inventory it
    believes is empty.

    ``_register_observed_lora_refs`` runs BEFORE the write, so any ref
    the pod reports back is redactable by the time it can appear in a
    log line or a traceback.

    Args:
        ledger: Duck-typed ledger, or ``None`` to skip the write.
        pod_id: The pod whose row is updated.
        resp: The ``set_lora_stack`` response body.
    """
    if ledger is None:
        return
    inventory = resp.get("inventory", []) if isinstance(resp, dict) else []
    _register_observed_lora_refs({"inventory": inventory})
    inventory_dicts = [_entry_to_dict(e) for e in inventory]
    free_bytes = resp.get("free_bytes") if isinstance(resp, dict) else None
    try:
        ledger.touch(
            pod_id,
            lora_inventory=inventory_dicts,
            loras_dir_free_bytes=int(free_bytes) if free_bytes is not None else 0,
            loras_dir_free_bytes_observed_at_local=datetime.now().isoformat(),
        )
    except Exception as touch_exc:  # noqa: BLE001 — bookkeeping, not the apply
        # The ONLY ``except`` in this module, and deliberately not in the
        # apply path: by the time it can fire the pod has already
        # accepted the stack, so the generation about to run is correct.
        # Killing that run — after paying for the boot and the download —
        # because a ledger write failed would be disproportionate; the
        # orchestrator treats its own ``Ledger.touch`` failures the same
        # way (B3, ``orchestrator.py``). The cost is an under-reported
        # row, which is what the WARNING names.
        _log.warning(
            "lora-apply: pod %s loaded the stack but its ledger row was NOT "
            "updated (%s); `kinoforge list` will under-report this pod's "
            "LoRA inventory until the next swap",
            pod_id,
            touch_exc,
        )
