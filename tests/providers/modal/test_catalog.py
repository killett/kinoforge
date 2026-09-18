"""Behavior: the static Modal GPU catalog and requirement-filtering."""

from kinoforge.core.interfaces import Placement
from kinoforge.providers.modal._catalog import MODAL_GPU_CATALOG, modal_offers


def test_catalog_has_expected_gpu_strings_and_vram():
    # Bug caught: using the AWS-ism "A10G" (Modal rejects it) or wrong VRAM.
    by_id = {o.id: o for o in MODAL_GPU_CATALOG}
    assert "A10" in by_id and "A10G" not in by_id
    assert by_id["A10"].vram_gb == 24
    assert by_id["A100-80GB"].vram_gb == 80
    assert {"T4", "L4", "A10", "L40S", "A100-40GB", "A100-80GB", "H100"} <= set(by_id)


def test_all_offers_are_serverless_mode():
    # Bug caught: mode="pod" would make filter_offers apply the $/hr cap and
    # silently drop pricier GPUs Modal can actually serve.
    assert all(o.mode == "serverless" for o in MODAL_GPU_CATALOG)


def test_modal_offers_filters_by_vram_and_orders_by_preference():
    reqs = Placement(min_vram_gb=40, accelerators=("A100-80GB", "A100-40GB"))
    offers = modal_offers(reqs)
    ids = [o.id for o in offers]
    assert "T4" not in ids and "A10" not in ids  # 16/24GB dropped by min_vram
    assert ids[0] == "A100-80GB"  # preference ordering wins


def test_h200_row_matches_modal_published_specs():
    # Bug caught: a vram_gb inferred from a datasheet rather than stated by
    # Modal. That is the U48 shape — a constant the provider never supplied,
    # which filter_offers then applies silently. Modal's GPU docs state
    # H200 = "141 GB"; its pricing page states $0.001261/sec = $4.54/hr.
    by_id = {o.id: o for o in MODAL_GPU_CATALOG}
    assert "H200" in by_id
    assert by_id["H200"].vram_gb == 141
    assert by_id["H200"].cost_rate_usd_per_hr == 4.54
    assert by_id["H200"].mode == "serverless"


def test_h200_is_the_only_card_above_80gb():
    # Bug caught: helpfully adding B200/B300 alongside H200. Modal's docs do
    # NOT state their VRAM, so any number written for them is invented.
    big = {o.id for o in MODAL_GPU_CATALOG if o.vram_gb > 80}
    assert big == {"H200"}


def test_a_request_above_80gb_is_now_satisfiable():
    # Bug caught: the row exists but filter_offers still drops it, so the
    # catalog looks right and nothing can actually book it. Before this
    # change this returned [] — that emptiness is what blocked MiniMax-H3.
    offers = modal_offers(Placement(min_vram_gb=120))
    assert offers, "no Modal offer satisfies min_vram_gb=120"
    assert all(o.vram_gb >= 120 for o in offers)
    assert offers[0].id == "H200"


def test_h200_ranks_first_when_a_config_asks_for_it_by_name():
    # Bug caught: a config naming H200 gets something else at candidates[0].
    # The test above passes trivially because H200 is the only card over
    # 120 GB; this one exercises the RANKING path with a low vram floor, so
    # H200 must beat seven cheaper cards that all clear the bar.
    offers = modal_offers(Placement(min_vram_gb=16, accelerators=("H200",)))
    assert offers[0].id == "H200"
    assert len(offers) > 1, "expected the cheaper cards to remain as fallbacks"
