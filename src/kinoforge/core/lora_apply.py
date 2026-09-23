"""Apply the resolved LoRA stack to a ready pod, before the first job.

This module closes the defect the H3 LoRA shared seam exists to remove:
a ``loras:`` block aimed at a pod that never loaded it used to produce a
plausible-looking video with no LoRA in it, and nothing anywhere said
so. The rule here is absolute — **a stack that did not load costs an
error, never a silent generation.** Every failure raised by the backend
propagates untouched; nothing is downgraded to a warning.

Download specs are resolved CONTROLLER-SIDE and shipped to the pod as
plain ``{url, headers, filename, size_hint}`` dicts. That is why a
CivitAI or HuggingFace token never has to exist on a pod: the
controller mints the authenticated URL + headers and the pod just
fetches them.

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

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from kinoforge.core import registry
from kinoforge.core.errors import ValidationError
from kinoforge.core.logging import get_logger

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


def ensure_lora_stack(
    *,
    backend: object,
    cfg: Any,  # noqa: ANN401 — a Config; typed loosely to avoid an import cycle
    pod_id: str | None,
    creds: CredentialProvider | None,
) -> None:
    """Apply cfg/vault/CLI's resolved LoRA stack to a ready pod.

    Called from ``orchestrator.deploy_session`` once the pod reports
    ready and before any job is submitted, so cold pods and
    caller-supplied warm pods take the identical path.

    No-ops when the stack is empty, when the backend has no
    ``set_lora_stack`` (hosted engines), or when there is no pod to
    address. Any failure propagates: a stack that did not load must
    fail the run rather than silently produce a LoRA-less video.

    Args:
        backend: The generation backend for this session. Duck-typed —
            a backend without ``set_lora_stack`` is a no-op.
        cfg: The loaded kinoforge configuration.
        pod_id: Identifier of the ready pod, or ``None`` on a hosted
            path where there is no pod.
        creds: Credential provider used to resolve download specs.
            ``None`` falls back to the environment, matching
            ``deploy_session``'s own optional-creds contract.

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

    if not stack:
        # Zero HTTP calls for runs without LoRAs: an unconditional POST
        # would 404 against every server build with no LoRA surface.
        return

    if not isinstance(backend, SupportsSetLoraStack):
        # Hosted engines cannot load LoRAs at all. Not fatal — but not
        # silent either: the discard is named, by count.
        _log.warning(
            "lora-apply: backend %s exposes no set_lora_stack; %d LoRA "
            "entries in this config will NOT be applied",
            type(backend).__name__,
            len(stack),
        )
        return

    if pod_id is None:
        _log.warning(
            "lora-apply: no pod to address; %d LoRA entries will NOT be applied",
            len(stack),
        )
        return

    _log.info("lora-apply: applying %d LoRA entries to pod %s", len(stack), pod_id)
    specs = resolve_download_specs(
        [lo.ref for lo in stack], creds or EnvCredentialProvider()
    )
    # Bare call, no try/except: every LoraSwapError subclass must reach
    # the operator. Swallowing one here is exactly the silent defect
    # this module was written to remove.
    backend.set_lora_stack(pod_id=pod_id, active_stack=stack, download_specs=specs)
    _log.info("lora-apply: pod %s accepted the %d-entry stack", pod_id, len(stack))
