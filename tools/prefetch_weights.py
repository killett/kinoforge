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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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


class PrefetchNotDurable(RuntimeError):
    """The download succeeded in the container but did not persist.

    Distinct from :class:`PrefetchPlanError`, which is a bad request caught for
    $0 before anything is booked. This one means real money was spent and the
    bytes are not there — so the next run on the expensive card would download
    them again.
    """


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


#: Modal SDK credentials. Named here so the pre-flight can report WHICH one is
#: absent; their VALUES are never read, logged, or interpolated.
_MODAL_CRED_VARS = ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET")


def check_modal_credentials(*, load_dotenv: bool = True) -> None:
    """Fail early and legibly when Modal credentials are absent.

    ``pixi run`` does NOT source the project dotenv file — pixi 0.69 fires
    activation scripts *before* reading it — so a tool that assumes otherwise
    reaches the Modal SDK unauthenticated and dies in a long traceback whose
    final line is "Token missing", naming neither the dotenv file nor the
    variable. Observed live on 2026-09-17. This converts that into one
    actionable line, raised before anything is booked.

    Args:
        load_dotenv: Load the project dotenv file into ``os.environ`` first.
            ``False`` in tests, which drive ``os.environ`` directly.

    Raises:
        PrefetchPlanError: One or both credential variables are unset. The
            message names the missing VARIABLES and never their values.
    """
    import os

    if load_dotenv:
        from kinoforge.core.dotenv_loader import load_env_file

        load_env_file()

    missing = [v for v in _MODAL_CRED_VARS if not os.environ.get(v)]
    if missing:
        raise PrefetchPlanError(
            f"Modal credentials absent: {', '.join(missing)} "
            f"(required: {', '.join(_MODAL_CRED_VARS)}). Note that `pixi run` "
            "does not source the project dotenv file by itself — this tool "
            "loads it explicitly, so an unset variable here means it is "
            "genuinely absent rather than merely unsourced."
        )


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


#: Why there is no controller-side byte check here, stated so the next reader
#: does not rebuild one. It is tempting to verify durability by listing
#: ``hub/models--<repo>/blobs`` from the controller and summing ``size``. That
#: measure is WRONG and it produced a false alarm on 2026-09-18: it reported
#: 1.96 GB for a MiniMax-H3 tree that a fresh container measured at 288.10 GB
#: with zero broken symlinks.
#:
#: Two reasons, and the second one is NOT what I first wrote. Modal's
#: ``listdir`` reports 0 for a symlink rather than its target's size — and a
#: snapshot entry IS a symlink. And the bytes are not under the per-model
#: ``blobs/`` directory at all: ``os.path.realpath`` on a shard resolves to
#: ``hub/blobs/<2-hex>/<sha256>`` — a SHARED, two-level-sharded store beside
#: the model directories, not inside one. (My first explanation blamed xet.
#: It is not xet; it is simply the wrong directory.) The per-model ``blobs/``
#: holds only small files, which is why it summed to a plausible-looking 1.96
#: GB instead of to zero, and why the Wan repos happen to sum correctly — that
#: coincidence is what made the bad measure convincing.
#:
#: Durability means "a NEW container mounting the Volume sees the bytes", so
#: that is what :func:`verify_resident` asks, in its own app run.
_DURABILITY_NOTE = __doc__


def verify_resident(
    repo_id: str,
    *,
    reported_bytes: int,
    remeasure: Callable[[], int],
    tolerance: float = 0.98,
) -> int:
    """Confirm the fetch is durable by re-measuring it from a fresh container.

    Two independent observations, which is the point: the fetch container walks
    the snapshot and reports a size, then a SECOND container — new process, new
    mount, after the first app has stopped — walks it again. A commit that did
    not persist shows up as a gap.

    It has to be a container. A controller-side listing cannot see through the
    HF cache's symlinks, and cannot see xet-backed content at all; see
    ``_DURABILITY_NOTE``.

    Args:
        repo_id: HuggingFace repo id, for the error message.
        reported_bytes: What the fetch container measured.
        remeasure: Callable returning the bytes a fresh container sees.
        tolerance: Fraction of *reported_bytes* that must be resident. Slightly
            below 1.0 so an incidental difference between two walks does not
            fail a healthy run and get the guard deleted as noise.

    Returns:
        The re-measured byte count.

    Raises:
        PrefetchNotDurable: The fresh container sees less than *tolerance* of
            what the fetch reported.
    """
    durable = remeasure()
    if durable < reported_bytes * tolerance:
        raise PrefetchNotDurable(
            f"{repo_id}: the fetch reported {reported_bytes / 1e9:.2f} GB but a fresh "
            f"container sees {durable / 1e9:.2f} GB on the Volume. The fetch did not "
            "persist — re-run it here rather than discovering it as a re-download on "
            "the expensive card. Check that the remote function calls volume.commit() "
            "before returning."
        )
    return durable


def fetch_snapshot(  # noqa: ANN401 — `volume` is a modal.Volume; modal is pod-side only
    repo_id: str,
    patterns: list[str],
    mount: str,
    volume: Any,  # noqa: ANN401 — a modal.Volume; modal is not importable in the default env
    *,
    download: Callable[..., str] | None = None,
    measure: Callable[[str], int] | None = None,
) -> tuple[str, int]:
    """Download *patterns* of *repo_id* into *mount* and COMMIT the Volume.

    Runs INSIDE the Modal container. Extracted from the remote function body so
    the commit is reachable from a test with a fake volume — the 2026-09-17
    defect was an absent commit, and an absent call is exactly what a test of
    the enclosing ``run_prefetch`` could not see.

    The commit is deliberately in a ``finally``: the download is the expensive
    part, so once it has happened the bytes are made durable before anything
    optional (the size walk) can raise and discard them.

    Args:
        repo_id: HuggingFace repo id.
        patterns: ``allow_patterns`` for ``snapshot_download``.
        mount: Where the Volume is mounted; also ``HF_HOME``.
        volume: The mounted ``modal.Volume``.
        download: Download seam; defaults to ``snapshot_download``.
        measure: Size seam; defaults to a symlink-following walk of the
            snapshot tree.

    Returns:
        ``(snapshot_path, total_bytes)``.
    """
    import os

    # hf_transfer gives a large speedup on multi-GB pulls; the whole point of
    # this tool is minimising billed seconds.
    os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")
    os.environ["HF_HOME"] = mount

    if download is None:
        from huggingface_hub import snapshot_download

        download = snapshot_download
    if measure is None:

        def measure(path: str) -> int:
            return sum(
                os.path.getsize(os.path.join(r, f))
                for r, _, fs in os.walk(path)
                for f in fs
            )

    path = download(repo_id, allow_patterns=patterns)
    try:
        return path, measure(path)
    finally:
        # INSIDE the container, which is what Modal's own error message said:
        # "commit() can only be called on a mounted volume inside a container".
        # `cdbd9087` read that as "do not commit" and deleted the call; the
        # instruction was to MOVE it. Without it the writes are not durable and
        # the tool reports a success that leaves the Volume nearly empty.
        volume.commit()


def run_prefetch(plan: PrefetchPlan) -> str:
    """Execute *plan* on Modal, commit the Volume, and verify it persisted.

    Imports ``modal`` lazily so the plan-building path — and its tests — stay
    importable in the default pixi env, which has no ``modal``.

    Args:
        plan: The validated plan.

    Returns:
        The remote function's summary string, plus the verified durable size.

    Raises:
        PrefetchNotDurable: The download did not persist to the Volume.
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
    def _fetch(repo_id: str, patterns: list[str], mount: str) -> tuple[str, int]:
        return fetch_snapshot(repo_id, patterns, mount, volume)

    with app.run():
        path, reported = _fetch.remote(
            plan.repo_id, list(plan.allow_patterns), plan.volume_mount
        )

    # The commit happens INSIDE the fetch container (see fetch_snapshot), and is
    # still not trusted on its own. A SECOND app — new container, new mount,
    # started after the first has stopped — re-walks the snapshot and reports
    # what it sees. That is what durability means here, and it is the only
    # measure that holds: a controller-side blob listing cannot see through the
    # HF cache's symlinks (Modal reports size 0 for one) and cannot see
    # xet-backed content at all. See _DURABILITY_NOTE.
    #
    # It runs on a CPU-only image: no GPU is needed to stat files, and at
    # $0.0000131/core/s this check costs a fraction of a cent.
    verify_app = modal.App(name=f"{app.name}-verify")

    @verify_app.function(  # type: ignore[untyped-decorator]
        image=modal.Image.debian_slim(),
        volumes={plan.volume_mount: volume},
        timeout=900,
        serialized=True,
    )
    def _remeasure(snapshot_path: str) -> int:
        import os

        total = 0
        for root, _, files in os.walk(snapshot_path):
            for name in files:
                # getsize FOLLOWS symlinks, so a broken link raises here rather
                # than quietly contributing zero — which is the failure this
                # whole check exists to surface.
                total += os.path.getsize(os.path.join(root, name))
        return total

    with verify_app.run():
        durable = verify_resident(
            plan.repo_id,
            reported_bytes=reported,
            remeasure=lambda: int(_remeasure.remote(path)),
        )
    return (
        f"{path} :: reported {reported / 1e9:.2f} GB, "
        f"re-measured from a fresh container {durable / 1e9:.2f} GB"
    )


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

    # Credentials are checked AFTER the plan renders (so --dry-run stays
    # usable with no secrets present) but BEFORE anything is booked.
    try:
        check_modal_credentials()
    except PrefetchPlanError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(run_prefetch(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
