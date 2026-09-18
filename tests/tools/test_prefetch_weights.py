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
