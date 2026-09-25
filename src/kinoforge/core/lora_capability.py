"""How — or whether — a generation engine can apply a LoRA stack.

U56. A hosted-engine config carrying ``loras:`` generated LoRA-less video. The
gate for that cannot be a pattern match on ``engine.kind``, because two shapes
look identical from the cfg and must be told apart:

* A ComfyUI config LEGITIMATELY carries ``loras:``. The block feeds
  ``capability_key()`` while the adapters are applied through workflow NODES,
  never through the ``/lora/set_stack`` seam. Rejecting it would break correct
  configs — which is why ``LoraServerSupportCheck`` declines to apply outside
  diffusers at all.
* A hosted engine carrying the same block can never apply it, and the run
  returns a plausible video with no LoRA in it.

So the engine declares its own answer, the way a ``ComputeProvider`` declares
``Capability.RATE_READBACK`` rather than having the enforcement point infer it
from the provider's name.
"""

from __future__ import annotations

from enum import StrEnum


class LoraSupport(StrEnum):
    """Whether an engine can apply a LoRA stack, and by what route.

    NONE        — the engine cannot apply adapters at all. A ``loras:`` block
                  or a ``--loras`` stack is an ERROR: it would be silently
                  dropped and the run would produce LoRA-less output that
                  looks fine. Hosted Bearer engines (Replicate, Runway, fal,
                  Bedrock) are here — the remote service owns the pipeline and
                  exposes no adapter surface this project drives.
    SERVER_HTTP — applied by ``core/lora_apply.ensure_lora_stack`` over the
                  pod's ``/lora/set_stack`` route. Diffusers. This is the only
                  value for which that seam's "NOT APPLYING" warning is a real
                  alarm; anywhere else it is noise about a discrepancy that
                  does not exist.
    WORKFLOW    — applied by the engine's own graph, outside this seam.
                  ComfyUI. A ``loras:`` block is legitimate and must not be
                  refused, but ``ensure_lora_stack`` is correct to do nothing
                  with it and must not warn about doing so.
    """

    NONE = "NONE"
    SERVER_HTTP = "SERVER_HTTP"
    WORKFLOW = "WORKFLOW"

    @property
    def can_apply(self) -> bool:
        """Return whether a declared stack reaches the model at all.

        Returns:
            True for SERVER_HTTP and WORKFLOW; False for NONE.
        """
        return self is not LoraSupport.NONE
