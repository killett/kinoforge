"""LIVE U3: a pod's endpoint URL is reachable from a FRESH process. PROVEN 2026-09-09.

U3's claim cannot be proven offline, and the reason is structural rather than
effort: it is a claim about what a process that did NOT create the pod can
resolve. Nothing offline exercises that against a real provider —
``LocalProvider`` keeps its instances in-process (so a fresh process raises
``KeyError`` before the endpoint render is ever reached) and Modal's / RunPod's
``get_instance`` are network calls. Hence this file.

The fix (``e033b170``) was proven offline at the unit and call-site level; what
was owed here was the end-to-end claim against a live pod.

**RESULT: PASS, 2026-09-09, $0.055** — Modal A10, pod ``run-20260909-182423``,
alive 18:24:23 -> 18:27:26 (3 m 03 s). All three criteria met from processes
that did not create the pod:

1. ``status --id`` printed
   ``endpoints={"8000": "https://<operator>--kinoforge-run-20260909-182423-build-modal-a-84522e.modal.run"} (recorded at launch, not verified live)``
   at exit 0, where pre-fix it printed ``unknown (no live endpoint)``. The
   label was present, so the F11 guard held.
2. ``pod lora ls`` exited 0 with ``no LoRAs loaded`` — an HTTP request really
   reached the pod — where pre-fix it exited 2 with ``no endpoint URL``.
3. Same host triangulated: Modal's own deploy URL is byte-identical to the one
   ``status`` reported; a direct ``GET /lora/inventory`` on that host returned
   HTTP 200 ``{'inventory': [], ...}``; and the negative control
   ``pod lora ls run-does-not-exist`` exited 1 ``not found in ledger``, so the
   command refuses rather than fabricating a host (the U4 shape).

Utilisation was polled throughout, never ``est_spend``: gpu 98% at uptime 21 s,
100% mid-generation, 0.0% once the clip published — and that 0% is what
triggered teardown rather than letting an idle pod bill. Frame-QA PASS with
soft flags. Teardown verified from fresh processes after the orchestrator
exited: both ``kinoforge list`` lines, ``modal app list`` state ``stopped`` with
0 tasks, no ephemeral index.

Trap worth keeping: the generate process was a ZOMBIE (``Z``/defunct) while
unreaped, so ``kill -0 <pid>`` reported it alive and briefly mislabelled a
post-exit read as concurrent. ``kill -0`` succeeds on zombies — check the
process STATE, not signal zero, to decide whether a run has finished.

Full evidence is in the U3 entry of ``PROGRESS.md``; the generation itself is a
See-also under section 22 of ``successful-generations.md`` (same capability
tuple, so no new section).

Driven manually via the CLI, matching the convention of the other live contract
files in this directory; this module records the contract and the pass criteria.

Deliberate deviation, recorded so it is not mistaken for the money leak the rule
exists to prevent: ``GEN_CMD`` does NOT pass ``--no-reuse``. The project rule
requires it for one-shot smokes because a surviving pod is a money leak — but
here the surviving pod IS the subject. ``--no-reuse`` destroys the pod when
generation ends, which would leave nothing for a fresh process to resolve and
make the proof impossible to run. The rule's own carve-out is a deliberate
multi-run warm session; this is the same class. The obligations that come with
it are honoured explicitly below: poll utilisation during the run, destroy
explicitly afterwards, and verify teardown from a fresh process.
"""

import pytest

pytestmark = pytest.mark.live

CFG = "examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml"

#: Boots a real Modal A10 and leaves it warm. Prompt comes from the cfg, which
#: carries the standard smoke prompt verbatim (memory:
#: feedback_standard_test_prompt) — no per-test override, so the clip stays
#: comparable with every other video-gen smoke.
GEN_CMD = (
    "pixi run -e live-modal kinoforge generate "
    f'--config {CFG} --mode t2v --prompt "$(cat examples/configs/prompts/'
    'field-realistic.txt)"'
)

#: Both reads run in processes that did NOT create the pod. That is the whole
#: point: before the fix each of these could only be answered by the creating
#: process, because ``ModalProvider.endpoints`` falls back to a per-process
#: ``_deployments`` dict and ``get_instance`` builds the Instance from
#: ``modal app list``, which carries no URL.
STATUS_CMD = "pixi run -e live-modal kinoforge status --id <POD_ID>"
LORA_LS_CMD = "pixi run -e live-modal kinoforge pod lora ls <POD_ID>"

TEARDOWN_CMD = "pixi run -e live-modal kinoforge destroy --id <POD_ID>"
VERIFY_CMD = "pixi run -e live-modal kinoforge list"


@pytest.mark.xfail(reason="PASSED live 2026-09-09; CLI-driven, see PROGRESS U3 entry")
def test_u3_endpoint_resolves_from_a_fresh_process_contract() -> None:
    """Contract for the owed U3 live re-proof.

    Pass criteria, all from processes that did not create the pod:

    1. ``STATUS_CMD`` prints the pod's real ``.modal.run`` URL in its
       ``endpoints=`` field. Before the fix this read
       ``endpoints=unknown (no live endpoint)`` while that exact URL was
       serving traffic. Because ``status`` uses the PURE read and Modal cannot
       confirm the URL out-of-process, the URL must carry the
       ``(recorded at launch, not verified live)`` label — an unlabelled render
       here would be the F11 failure (a possibly-dead endpoint presented as
       current), so the label is part of the pass, not a cosmetic detail.
    2. ``LORA_LS_CMD`` reaches the pod over HTTP. Before the fix it exited 2
       with ``pod lora ls: no endpoint URL for pod <id>``. Any outcome that
       proves an HTTP request was actually issued against the recorded URL
       counts — an empty inventory (this cfg loads no LoRAs) is a PASS. The
       failure that would NOT count is the old "no endpoint URL" message, which
       means resolution never happened.
    3. The URL that ``status`` reports and the URL ``pod lora ls`` requests are
       the SAME host, and it is the host Modal actually served. Asserting the
       two independently would let a fabricated-but-consistent hostname pass —
       the shape of defect U4 was exactly that.

    Obligations that ride along with the deliberate warm run:

    4. Utilisation is polled during generation via the Modal ``/util`` route
       (``gpuUtilPercent``), never ``est_spend`` — a spend figure climbs
       identically whether the GPU is at 100% or dead.
    5. The output clip gets frame-extraction visual QA before the run is called
       green. Exit code and ffprobe dimensions cannot see pixels.
    6. Teardown is explicit and verified AFTER the orchestrator exits:
       ``VERIFY_CMD`` prints both "no instances" lines and ``modal app list``
       shows no non-stopped kinoforge app.
    """
    raise AssertionError(
        "run GEN_CMD live (warm, deliberately no --no-reuse), then STATUS_CMD "
        "and LORA_LS_CMD from fresh processes; assert the recorded .modal.run "
        "URL appears in status with the 'recorded at launch, not verified live' "
        "label, that pod lora ls reaches that same host over HTTP rather than "
        "reporting 'no endpoint URL', then TEARDOWN_CMD + VERIFY_CMD clean"
    )
