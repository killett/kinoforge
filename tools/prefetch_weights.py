r"""Prefetch HuggingFace weights onto the shared Modal Volume, cheaply.

Why this exists
---------------
``kinoforge-hf-cache`` (see ``providers/modal/_app.py``) is mounted at
``HF_HOME`` on every Modal pod, so weights persist across runs. But there is
no way to POPULATE it except by running the job that needs it — which means
the first run of a large model downloads on whatever GPU that job booked.

For MiniMax-H3 that is 144.1 GB on an H200 at $4.54/hr (~$3.94), and an
interrupted fetch pays it again. Running the identical download on the
cheapest card in the catalog costs roughly a tenth of that, and a failed
attempt costs cents. HuggingFace's cache dedupes by blob hash, so a retry
re-fetches only what is missing.

Usage::

    pixi run -e live-modal python tools/prefetch_weights.py \
        --repo MiniMaxAI/MiniMax-H3 \
        --include model_index.json 'FL2VA/*'

Pass ``--dry-run`` to print the resolved plan and exit 0 without booking
anything.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from kinoforge.providers.modal._app import _VOLUME_NAME
from kinoforge.providers.modal._catalog import MODAL_GPU_CATALOG

#: Where the Volume is mounted inside the prefetch container. The provider
#: exports this same path as ``HF_HOME`` on generation pods, so the blobs this
#: tool writes land exactly where ``snapshot_download`` will later look.
VOLUME_MOUNT = "/cache/hf"

#: 144.1 GB at Modal's observed throughput runs ~50 min. Modal's own default
#: function timeout is 300 s and has already killed a kinoforge container
#: mid-download once (see the startup_timeout note in ``_app.py``), so this
#: floor is deliberately generous — a prefetch that dies at 95% has bought
#: nothing but still billed.
DEFAULT_TIMEOUT_S = 7200


class PrefetchPlanError(ValueError):
    """The requested prefetch would download the wrong bytes.

    Raised at plan time, before anything is booked, so a malformed request
    costs $0 rather than being discovered on a GPU.
    """


@dataclass(frozen=True)
class PrefetchPlan:
    """A resolved, validated prefetch request.

    Frozen so a caller cannot mutate the patterns after validation has
    approved them.

    Attributes:
        repo_id: HuggingFace repo, e.g. ``"MiniMaxAI/MiniMax-H3"``.
        allow_patterns: Glob patterns passed to ``snapshot_download``. Never
            empty — an empty value pulls the entire repo.
        gpu: Modal GPU string for the prefetch container.
        volume_name: The shared Modal Volume to write.
        volume_mount: Mount path inside the container, also ``HF_HOME``.
        timeout_s: Function timeout for the download.
    """

    repo_id: str
    allow_patterns: tuple[str, ...]
    gpu: str
    volume_name: str
    volume_mount: str
    timeout_s: int


def _cheapest_gpu() -> str:
    """Return the id of the lowest-priced GPU in the Modal catalog.

    Derived rather than hardcoded so that repricing, or adding a cheaper
    card, cannot silently leave this tool booking an expensive one.

    Returns:
        The Modal GPU string, e.g. ``"T4"``.
    """
    return min(MODAL_GPU_CATALOG, key=lambda o: o.cost_rate_usd_per_hr).id


def build_plan(
    repo_id: str,
    allow_patterns: tuple[str, ...],
    *,
    gpu: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> PrefetchPlan:
    """Validate a prefetch request and resolve it into a :class:`PrefetchPlan`.

    Two rules, both learned the expensive way:

    1. ``allow_patterns`` may not be empty. ``snapshot_download`` with no
       patterns pulls the whole repo — 498 GB rather than 144.1 GB for
       MiniMax-H3.
    2. A subfolder glob must be accompanied by at least one repo-root pattern.
       ``ModularPipeline.from_pretrained`` loads the repo ROOT, so fetching
       ``FL2VA/*`` alone produces a download that looks complete and does not
       load — and that failure surfaces on the expensive GPU, never here.

    Args:
        repo_id: HuggingFace repo id.
        allow_patterns: Glob patterns to fetch.
        gpu: Override the GPU. Defaults to the cheapest catalog entry.
        timeout_s: Function timeout for the download.

    Returns:
        The validated plan.

    Raises:
        PrefetchPlanError: ``allow_patterns`` is empty, or a subfolder glob
            appears with no repo-root pattern beside it.
    """
    patterns = tuple(allow_patterns)
    if not patterns:
        raise PrefetchPlanError(
            "allow_patterns is empty — snapshot_download would pull the ENTIRE "
            f"repo {repo_id!r}. Pass the specific paths you need."
        )

    has_subfolder = any("/" in p for p in patterns)
    has_root_file = any("/" not in p for p in patterns)
    if has_subfolder and not has_root_file:
        raise PrefetchPlanError(
            f"allow_patterns {list(patterns)} fetches a subfolder but no "
            "repo-root file. The root model_index.json is what "
            "ModularPipeline.from_pretrained loads, so this download would "
            "look complete and fail to load — on the GPU, not here. Add "
            '"model_index.json" (or the root manifest this repo uses).'
        )

    return PrefetchPlan(
        repo_id=repo_id,
        allow_patterns=patterns,
        gpu=gpu if gpu is not None else _cheapest_gpu(),
        volume_name=_VOLUME_NAME,
        volume_mount=VOLUME_MOUNT,
        timeout_s=timeout_s,
    )


def _render(plan: PrefetchPlan) -> str:
    """Return a human-readable summary of *plan* for the operator.

    Args:
        plan: The resolved plan.

    Returns:
        A multi-line string.
    """
    rate = next(
        (o.cost_rate_usd_per_hr for o in MODAL_GPU_CATALOG if o.id == plan.gpu),
        None,
    )
    rate_str = f"${rate:.2f}/hr" if rate is not None else "unknown rate"
    return "\n".join(
        (
            "prefetch plan:",
            f"  repo:      {plan.repo_id}",
            f"  patterns:  {list(plan.allow_patterns)}",
            f"  gpu:       {plan.gpu} ({rate_str})",
            f"  volume:    {plan.volume_name} -> {plan.volume_mount}",
            f"  timeout:   {plan.timeout_s}s",
        )
    )


def run_prefetch(plan: PrefetchPlan) -> str:
    """Execute *plan* on Modal and commit the Volume.

    Imports ``modal`` lazily so the plan-building path — and its tests — stay
    importable in the default pixi env, which has no ``modal``.

    Args:
        plan: The validated plan.

    Returns:
        The remote function's summary string.
    """
    import modal

    app = modal.App(name=f"kinoforge-prefetch-{plan.repo_id.split('/')[-1].lower()}")
    volume = modal.Volume.from_name(plan.volume_name, create_if_missing=True)
    image = modal.Image.debian_slim().pip_install("huggingface_hub[hf_transfer]")

    @app.function(  # type: ignore[untyped-decorator]  # decorator from an Any-typed module
        gpu=plan.gpu,
        # image= is load-bearing: without it the container is Modal's bare
        # default and `import huggingface_hub` fails at call time, on a booked
        # GPU. Ruff caught exactly this as F841 when the variable was built
        # and never passed.
        image=image,
        volumes={plan.volume_mount: volume},
        timeout=plan.timeout_s,
        serialized=True,
        secrets=[modal.Secret.from_dict({"HF_HOME": plan.volume_mount})],
    )
    def _fetch(repo_id: str, patterns: list[str], mount: str) -> str:
        import os

        # hf_transfer gives a large speedup on multi-GB pulls; the whole point
        # of this tool is minimising billed seconds.
        os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")
        os.environ["HF_HOME"] = mount
        from huggingface_hub import snapshot_download

        path = snapshot_download(repo_id, allow_patterns=patterns)
        total = sum(
            os.path.getsize(os.path.join(r, f))
            for r, _, fs in os.walk(path)
            for f in fs
        )
        return f"{path} :: {total / 1e9:.2f} GB"

    with app.run():
        result: str = _fetch.remote(
            plan.repo_id, list(plan.allow_patterns), plan.volume_mount
        )
    volume.commit()
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: 0 on success, 2 on a rejected plan.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, help="HuggingFace repo id")
    ap.add_argument(
        "--include",
        required=True,
        nargs="+",
        metavar="GLOB",
        help='paths to fetch, e.g. model_index.json "FL2VA/*"',
    )
    ap.add_argument("--gpu", default=None, help="override the prefetch GPU")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved plan and exit 0 without booking anything",
    )
    args = ap.parse_args(argv)

    try:
        plan = build_plan(
            args.repo,
            tuple(args.include),
            gpu=args.gpu,
            timeout_s=args.timeout,
        )
    except PrefetchPlanError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(_render(plan))
    if args.dry_run:
        return 0
    print(run_prefetch(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
