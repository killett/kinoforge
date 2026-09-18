"""Behavior: the HF-weights prefetch plan is cheap, correct, and complete.

This tool exists to move a large model download OFF the expensive GPU that
needs the weights and ONTO the cheapest container that can write the shared
Modal Volume. Every test here guards one of the three ways that can silently
fail to save anything: fetching the wrong bytes, fetching them to the wrong
place, or fetching them on the wrong card.
"""

from __future__ import annotations

import pytest

from kinoforge.providers.modal._app import _VOLUME_NAME
from kinoforge.providers.modal._catalog import MODAL_GPU_CATALOG
from tools.prefetch_weights import (
    PrefetchPlanError,
    build_plan,
    check_modal_credentials,
)

_H3 = "MiniMaxAI/MiniMax-H3"


def test_a_subfolder_glob_without_the_root_manifest_is_refused() -> None:
    # Bug caught: the operator-reported defect. `FL2VA/*` alone omits the
    # repo-root model_index.json, so 144.1 GB downloads, looks complete, and
    # then fails to load — on the $4.54/hr card, not on the cheap prefetch.
    # ModularPipeline.from_pretrained loads the repo ROOT, which is why the
    # documented command is --include "model_index.json" "FL2VA/*".
    with pytest.raises(PrefetchPlanError) as exc:
        build_plan(_H3, ("FL2VA/*",))
    assert "model_index.json" in str(exc.value)


def test_the_documented_pattern_pair_is_accepted_in_order() -> None:
    # Bug caught: a validator that over-rejects anything containing "/" would
    # refuse the only correct invocation; and a plan that silently dropped or
    # reordered patterns would fetch the wrong subset.
    plan = build_plan(_H3, ("model_index.json", "FL2VA/*"))
    assert plan.allow_patterns == ("model_index.json", "FL2VA/*")
    assert plan.repo_id == _H3


def test_empty_allow_patterns_is_refused() -> None:
    # Bug caught: no allow_patterns means snapshot_download pulls the WHOLE
    # repo — 498 GB instead of 144.1 GB (both measured from the HF API
    # 2026-09-17), i.e. 3.5x the wall-clock and 3.5x the money.
    with pytest.raises(PrefetchPlanError) as exc:
        build_plan(_H3, ())
    assert "allow_patterns" in str(exc.value)


def test_root_only_patterns_need_no_manifest_pairing() -> None:
    # Bug caught: the manifest rule applied unconditionally would block a
    # legitimate flat-repo prefetch. The rule exists only because a SUBFOLDER
    # glob hides the root manifest; with no subfolder there is nothing to hide.
    plan = build_plan("some/flat-repo", ("config.json", "weights.safetensors"))
    assert plan.allow_patterns == ("config.json", "weights.safetensors")


def test_the_default_gpu_is_the_cheapest_the_catalog_offers() -> None:
    # Bug caught: defaulting to (or inheriting) a large card turns a ~$0.15
    # fetch into a ~$4 one. It is SILENT — the prefetch still succeeds, it
    # just costs 8x, which is the entire saving this tool exists to produce.
    # Derived from the catalog rather than hardcoded, so adding a cheaper card
    # or repricing T4 cannot leave this assertion stale.
    cheapest = min(MODAL_GPU_CATALOG, key=lambda o: o.cost_rate_usd_per_hr)
    plan = build_plan(_H3, ("model_index.json", "FL2VA/*"))
    assert plan.gpu == cheapest.id


def test_the_plan_targets_the_same_volume_the_generation_pods_mount() -> None:
    # Bug caught: the highest-consequence silent failure in this tool. If the
    # prefetch writes a DIFFERENT volume, the generation pod finds nothing and
    # re-downloads 144.1 GB on the expensive card — the prefetch bought
    # exactly nothing while reporting success. Asserted against the provider's
    # own constant so renaming the volume cannot desync the two sides.
    plan = build_plan(_H3, ("model_index.json", "FL2VA/*"))
    assert plan.volume_name == _VOLUME_NAME


def test_missing_modal_credentials_raise_a_readable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bug caught: `pixi run` does NOT source the dotenv file (pixi 0.69
    # activation fires before it is read), so the tool reached Modal with no
    # token and died in a 20-line SDK traceback ending in "Token missing" —
    # observed live 2026-09-17. That traceback names neither the dotenv file
    # nor which variable was absent. This turns it into one actionable line,
    # raised BEFORE anything is booked.
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    with pytest.raises(PrefetchPlanError) as exc:
        check_modal_credentials(load_dotenv=False)
    msg = str(exc.value)
    assert "MODAL_TOKEN_ID" in msg
    assert "MODAL_TOKEN_SECRET" in msg


def test_present_modal_credentials_pass_the_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bug caught: a check eager enough to reject a correctly-configured
    # environment would block every real prefetch. Values are the repo's
    # synthetic placeholder convention, never real tokens.
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-deadbeef-placeholder")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "as-deadbeef-placeholder")
    check_modal_credentials(load_dotenv=False)


def test_the_credential_check_never_echoes_a_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Bug caught: an error message interpolating the variable's VALUE would
    # put a live credential into a durable transcript. Only one var is set
    # here, so the check must fail while saying nothing about the set one.
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-deadbeef-placeholder")
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    with pytest.raises(PrefetchPlanError) as exc:
        check_modal_credentials(load_dotenv=False)
    assert "deadbeef" not in str(exc.value)


def test_the_default_timeout_survives_a_144gb_fetch() -> None:
    # Bug caught: Modal's default function timeout is 300 s, and that default
    # has already killed a kinoforge container mid-download once (see the
    # startup_timeout note in providers/modal/_app.py). At 144.1 GB a 300 s
    # cap dies around 5 GB in, reporting "initializing for too long" rather
    # than anything about the download.
    plan = build_plan(_H3, ("model_index.json", "FL2VA/*"))
    assert plan.timeout_s >= 3600


# ---------------------------------------------------------------------------
# Durability — the fourth way this silently saves nothing
# ---------------------------------------------------------------------------
#
# The module docstring above names three failure modes: wrong bytes, wrong
# place, wrong card. There is a fourth: the download happens and does not
# persist. `cdbd9087` removed the controller-side `volume.commit()` after Modal
# answered it with `ConflictError: commit() can only be called on a mounted
# volume inside a container`, and concluded Modal commits on function return.
# The error was an instruction about WHERE, not whether — so the commit now
# happens inside the container.
#
# The verification is a second CONTAINER, not a controller-side byte count, and
# that distinction was learned the embarrassing way on 2026-09-18. Summing
# `hub/models--<repo>/blobs` from the controller reported 1.96 GB for a
# MiniMax-H3 tree that a fresh container then measured at 288.10 GB with zero
# broken symlinks: Modal's listdir reports 0 for a symlink rather than its
# target's size, and xet-backed content is not under `blobs/` at all. The Wan
# repos happen to sum correctly, which is what made the bad measure convincing.
# A guard that false-alarms on every healthy run is worse than no guard.


class _FakeVolume:
    """Records commits; stands in for a mounted modal.Volume."""

    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def test_the_fetch_commits_the_volume_from_inside_the_container() -> None:
    # Bug caught: without an explicit commit the writes are not guaranteed
    # durable, and the next H200 run would re-download 124 GiB at $4.54/hr —
    # the entire cost this tool exists to avoid. Modal's own error names the
    # fix: inside a container.
    from tools.prefetch_weights import fetch_snapshot

    volume = _FakeVolume()
    path, size = fetch_snapshot(
        "MiniMaxAI/MiniMax-H3",
        ["model_index.json", "transformer/*"],
        "/cache/hf",
        volume,
        download=lambda *a, **k: "/cache/hf/snap",
        measure=lambda _p: 144_050_000_000,
    )
    assert volume.commits == 1, "the volume was never committed inside the container"
    assert path == "/cache/hf/snap"
    assert size == 144_050_000_000


def test_the_fetch_commits_even_when_measuring_raises() -> None:
    # Bug caught: the commit is placed after the size walk, an os.walk error on
    # one file aborts the function, and a completed 144 GiB download is thrown
    # away for a diagnostic. The expensive work must be made durable BEFORE
    # anything optional runs against it.
    from tools.prefetch_weights import fetch_snapshot

    volume = _FakeVolume()

    def boom(_path: str) -> int:
        raise OSError("stat failed")

    with pytest.raises(OSError, match="stat failed"):
        fetch_snapshot(
            "MiniMaxAI/MiniMax-H3",
            ["model_index.json"],
            "/cache/hf",
            volume,
            download=lambda *a, **k: "/cache/hf/snap",
            measure=boom,
        )
    assert volume.commits == 1, "a completed download was discarded uncommitted"


def test_verify_resident_refuses_a_fetch_that_did_not_persist() -> None:
    # Bug caught: the fetch reports a large success and the Volume holds a
    # fraction of it. Exiting 0 there means the gap is discovered later as a
    # 124 GiB re-download on the H200 — and, worse, gets recorded as a
    # completed sub-project.
    from tools.prefetch_weights import PrefetchNotDurable, verify_resident

    with pytest.raises(PrefetchNotDurable) as exc:
        verify_resident(
            "MiniMaxAI/MiniMax-H3",
            reported_bytes=144_050_000_000,
            remeasure=lambda: 1_960_000_000,
        )
    message = str(exc.value)
    # Both numbers must appear: "it did not persist" is unactionable without the
    # gap, and the gap is what says re-run rather than debug the loader.
    assert "144" in message and "1.9" in message
    assert "MiniMaxAI/MiniMax-H3" in message


def test_verify_resident_accepts_a_fetch_that_actually_landed() -> None:
    # Bug caught: the tolerance is exact equality, so the normal case — two
    # walks of the same tree differing incidentally — fails every healthy run
    # and the guard gets deleted as noise.
    from tools.prefetch_weights import verify_resident

    assert (
        verify_resident(
            "MiniMaxAI/MiniMax-H3",
            reported_bytes=144_050_000_000,
            remeasure=lambda: 143_900_000_000,
        )
        == 143_900_000_000
    )


def test_verify_resident_measures_through_a_container_not_a_blob_listing() -> None:
    # Bug caught: THE 2026-09-18 false alarm. The check is reimplemented as a
    # controller-side sum of `hub/models--<repo>/blobs`, which reported 1.96 GB
    # for a tree a fresh container measured at 288.10 GB — Modal's listdir
    # reports 0 for a symlink, and xet-backed content is not under blobs/.
    # The guard then blocks every healthy prefetch.
    #
    # This pins the SEAM: the only way verify_resident learns a size is the
    # injected `remeasure` callable, so a future edit cannot quietly swap in a
    # listing-based measure without changing this signature.
    import inspect

    from tools.prefetch_weights import verify_resident

    params = inspect.signature(verify_resident).parameters
    assert "remeasure" in params
    assert "lister" not in params, (
        "a listing-based durability measure is wrong here — see the note above"
    )
    assert not hasattr(
        __import__("tools.prefetch_weights", fromlist=["x"]), "durable_bytes"
    ), "durable_bytes summed blobs/ from the controller and false-alarmed; it is gone"
