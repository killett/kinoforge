"""Behavior: the static Modal GPU catalog and requirement-filtering."""

from kinoforge.core.interfaces import HardwareRequirements
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
    reqs = HardwareRequirements(
        min_vram_gb=40, gpu_preference=("A100-80GB", "A100-40GB")
    )
    offers = modal_offers(reqs)
    ids = [o.id for o in offers]
    assert "T4" not in ids and "A10" not in ids  # 16/24GB dropped by min_vram
    assert ids[0] == "A100-80GB"  # preference ordering wins
