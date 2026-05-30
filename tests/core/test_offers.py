"""Tests for the pure offer-filtering helper."""

from kinoforge.core.interfaces import HardwareRequirements, Offer
from kinoforge.core.offers import filter_offers


def _o(
    id_: str, gpu: str, vram: int, cuda: str, cost: float, mode: str = "pod"
) -> Offer:
    return Offer(
        id=id_,
        gpu_type=gpu,
        vram_gb=vram,
        cuda=cuda,
        cost_rate_usd_per_hr=cost,
        mode=mode,
    )


def test_excludes_undersized_vram_and_old_cuda():
    offers = [
        _o("a", "RTX 4090", 24, "12.8", 1.0),  # vram too small
        _o("b", "RTX 4090", 48, "12.1", 1.0),  # cuda too old
        _o("c", "RTX 4090", 48, "12.8", 1.0),  # OK
    ]
    reqs = HardwareRequirements(
        min_vram_gb=48, min_cuda="12.8", max_cost_rate_usd_per_hr=2.20
    )
    assert [o.id for o in filter_offers(offers, reqs)] == ["c"]


def test_cuda_compare_is_semantic_not_string():
    # Bug this catches: string-comparing "12.10" vs "12.8" treats 12.10 as OLDER.
    offers = [_o("modern", "X", 48, "12.10", 1.0)]
    reqs = HardwareRequirements(
        min_vram_gb=48, min_cuda="12.8", max_cost_rate_usd_per_hr=2.20
    )
    assert [o.id for o in filter_offers(offers, reqs)] == ["modern"]


def test_cost_filter_excludes_pod_only_not_serverless():
    offers = [
        _o("pod_expensive", "X", 48, "12.8", 3.0, mode="pod"),
        _o("sl_expensive", "X", 48, "12.8", 3.0, mode="serverless"),
    ]
    reqs = HardwareRequirements(max_cost_rate_usd_per_hr=2.20)
    ids = [o.id for o in filter_offers(offers, reqs)]
    # Bug this catches: applying max_cost_rate uniformly would also exclude per-second
    # serverless offers, which the spec says use `budget` instead.
    assert "pod_expensive" not in ids
    assert "sl_expensive" in ids


def test_gpu_preference_orders_survivors():
    offers = [
        _o("a", "RTX 5090", 48, "12.8", 1.0),
        _o("b", "RTX 4090", 48, "12.8", 1.0),
    ]
    reqs = HardwareRequirements(gpu_preference=("RTX 4090", "RTX 5090"))
    # Bug this catches: dispatch order matching input order despite gpu_preference.
    assert [o.gpu_type for o in filter_offers(offers, reqs)] == ["RTX 4090", "RTX 5090"]


def test_empty_preference_preserves_input_order():
    offers = [
        _o("a", "RTX 5090", 48, "12.8", 1.0),
        _o("b", "RTX 4090", 48, "12.8", 1.0),
        _o("c", "H100", 48, "12.8", 1.0),
    ]
    reqs = HardwareRequirements()  # empty gpu_preference
    assert [o.id for o in filter_offers(offers, reqs)] == ["a", "b", "c"]


def test_unlisted_gpus_appended_after_preference():
    offers = [
        _o("a", "H100", 48, "12.8", 1.0),
        _o("b", "RTX 4090", 48, "12.8", 1.0),
        _o("c", "RTX 5090", 48, "12.8", 1.0),
    ]
    reqs = HardwareRequirements(gpu_preference=("RTX 4090",))
    # Bug this catches: dropping unlisted GPUs instead of appending them.
    out = [o.gpu_type for o in filter_offers(offers, reqs)]
    assert out[0] == "RTX 4090"
    assert set(out[1:]) == {"H100", "RTX 5090"}
    assert len(out) == 3
