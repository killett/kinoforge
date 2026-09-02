"""Tests for the pure offer-filtering helper."""

from kinoforge.core.interfaces import Offer, Placement
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
    reqs = Placement(min_vram_gb=48, min_cuda="12.8", max_usd_per_hr=2.20)
    assert [o.id for o in filter_offers(offers, reqs)] == ["c"]


def test_cuda_compare_is_semantic_not_string():
    # Bug this catches: string-comparing "12.10" vs "12.8" treats 12.10 as OLDER.
    offers = [_o("modern", "X", 48, "12.10", 1.0)]
    reqs = Placement(min_vram_gb=48, min_cuda="12.8", max_usd_per_hr=2.20)
    assert [o.id for o in filter_offers(offers, reqs)] == ["modern"]


def test_cost_filter_excludes_pod_only_not_serverless():
    offers = [
        _o("pod_expensive", "X", 48, "12.8", 3.0, mode="pod"),
        _o("sl_expensive", "X", 48, "12.8", 3.0, mode="serverless"),
    ]
    reqs = Placement(max_usd_per_hr=2.20)
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
    reqs = Placement(accelerators=("RTX 4090", "RTX 5090"))
    # Bug this catches: dispatch order matching input order despite gpu_preference.
    assert [o.gpu_type for o in filter_offers(offers, reqs)] == ["RTX 4090", "RTX 5090"]


def test_empty_preference_preserves_input_order():
    offers = [
        _o("a", "RTX 5090", 48, "12.8", 1.0),
        _o("b", "RTX 4090", 48, "12.8", 1.0),
        _o("c", "H100", 48, "12.8", 1.0),
    ]
    reqs = Placement()  # empty gpu_preference
    assert [o.id for o in filter_offers(offers, reqs)] == ["a", "b", "c"]


def test_unlisted_gpus_appended_after_preference():
    offers = [
        _o("a", "H100", 48, "12.8", 1.0),
        _o("b", "RTX 4090", 48, "12.8", 1.0),
        _o("c", "RTX 5090", 48, "12.8", 1.0),
    ]
    reqs = Placement(accelerators=("RTX 4090",))
    # Bug this catches: dropping unlisted GPUs instead of appending them.
    out = [o.gpu_type for o in filter_offers(offers, reqs)]
    assert out[0] == "RTX 4090"
    assert set(out[1:]) == {"H100", "RTX 5090"}
    assert len(out) == 3


def test_filter_offers_takes_a_placement_and_still_excludes_over_cap_pods() -> None:
    """The price filter is the pre-book half of the cap and must survive S4.

    Bug caught: dropping it because "the cap is verified after launch now"
    makes RunPod book an over-cap pod and then destroy it — paying for a boot
    that today never starts.
    """
    from kinoforge.core.interfaces import Placement

    cheap = Offer(
        id="a", gpu_type="A", vram_gb=80, cuda="12.8", cost_rate_usd_per_hr=0.50
    )
    dear = Offer(
        id="b", gpu_type="B", vram_gb=80, cuda="12.8", cost_rate_usd_per_hr=9.00
    )
    kept = filter_offers([cheap, dear], Placement(max_usd_per_hr=1.00))
    assert [o.id for o in kept] == ["a"]


def test_filter_offers_ranks_by_accelerators_not_gpu_preference() -> None:
    """Bug caught: reading a field that no longer exists silently ranks
    nothing, so an operator's ordered preference becomes input order."""
    from kinoforge.core.interfaces import Placement

    a = Offer(
        id="a", gpu_type="A100", vram_gb=80, cuda="12.8", cost_rate_usd_per_hr=1.0
    )
    h = Offer(
        id="h", gpu_type="H100", vram_gb=80, cuda="12.8", cost_rate_usd_per_hr=1.0
    )
    kept = filter_offers([a, h], Placement(accelerators=("H100", "A100")))
    assert [o.gpu_type for o in kept] == ["H100", "A100"]


def test_filter_offers_still_applies_the_vram_and_cuda_floors() -> None:
    """Bug caught: a signature swap that reads the wrong Placement field names
    would silently stop filtering — every offer survives and a 24 GB box gets
    booked for an 80 GB model, failing late at model load."""
    from kinoforge.core.interfaces import Placement

    small = Offer(
        id="s", gpu_type="T4", vram_gb=16, cuda="12.8", cost_rate_usd_per_hr=0.5
    )
    old = Offer(
        id="o", gpu_type="A100", vram_gb=80, cuda="12.0", cost_rate_usd_per_hr=0.5
    )
    good = Offer(
        id="g", gpu_type="A100", vram_gb=80, cuda="12.8", cost_rate_usd_per_hr=0.5
    )
    kept = filter_offers([small, old, good], Placement(min_vram_gb=80, min_cuda="12.8"))
    assert [o.id for o in kept] == ["g"]


def test_filter_offers_leaves_serverless_offers_above_the_cap_alone() -> None:
    """Boundary, and it is load-bearing for Modal: its whole catalog is
    mode="serverless". Bug caught: extending the pod price filter to
    serverless empties Modal's catalog for any cap below the GPU price, so
    every Modal launch fails with 'no offers'."""
    from kinoforge.core.interfaces import Placement

    dear = Offer(
        id="h",
        gpu_type="H100",
        vram_gb=80,
        cuda="12.8",
        cost_rate_usd_per_hr=3.95,
        mode="serverless",
    )
    assert filter_offers([dear], Placement(max_usd_per_hr=1.00)) == [dear]


def test_hardware_requirements_is_gone_from_the_seam() -> None:
    """S4 collapses the two overlapping resource types into the portable one.

    Bug caught: leaving the type importable lets a new provider keep taking a
    catalog filter, which is the RunPod-shaped assumption this stage exists to
    remove.
    """
    import kinoforge.core.interfaces as interfaces

    assert not hasattr(interfaces, "HardwareRequirements")
