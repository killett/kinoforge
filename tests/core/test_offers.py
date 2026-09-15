"""Tests for the pure offer-filtering helper."""

import pytest

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


# ---------------------------------------------------------------------------
# U43: an accelerator name the catalog does not carry must not be silent
# ---------------------------------------------------------------------------


def test_unknown_accelerator_name_warns_and_names_the_id_it_probably_meant(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """U43: a preference matching nothing in the catalog is reported, with a hint.

    Bug caught: ``rank()`` scores an unmatched name at ``len(accelerators)`` —
    the same rank as a GPU nobody asked for — so a wrong id is INERT and looks
    identical to a working cfg. Seven shipped cfgs name GPUs RunPod does not
    have; the 1.3B grid cfg's `NVIDIA RTX 4090` left the L4 leading its offer
    list, which is the whole of U36's nine-attempt create-then-destroy loop.

    The expected pair is RunPod's real catalog id, measured live 2026-09-14:
    the cfgs write ``NVIDIA RTX 4090`` and the catalog carries
    ``NVIDIA GeForce RTX 4090``. The suggestion is what makes the warning
    actionable rather than merely true — the two strings differ by one word in
    the middle, which is exactly the diff an operator's eye skips.
    """
    catalog = [
        _o("a", "NVIDIA GeForce RTX 4090", 24, "12.8", 0.74),
        _o("b", "NVIDIA L4", 24, "12.8", 0.49),
    ]
    placement = Placement(
        min_vram_gb=24,
        max_usd_per_hr=10.0,
        accelerators=(
            "NVIDIA RTX 4090",
            "NVIDIA L4",
        ),
    )

    with caplog.at_level("WARNING"):
        kept = filter_offers(catalog, placement)

    assert [o.gpu_type for o in kept] == ["NVIDIA L4", "NVIDIA GeForce RTX 4090"], (
        "ranking behaviour is unchanged by the warning — the unmatched name "
        "still sorts last, which is precisely why it needs saying out loud"
    )
    assert "NVIDIA RTX 4090" in caplog.text
    assert "NVIDIA GeForce RTX 4090" in caplog.text


def test_a_known_accelerator_priced_out_of_range_does_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A name the catalog DOES carry must stay silent, however it was filtered.

    Bug caught: computing the unknown set from the KEPT offers instead of the
    input catalog warns every time a named GPU is merely over cap or too small
    — which is the shipped 1.3B grid cfg's normal, correct behaviour, since its
    secure 4090 bills $0.74 against a $0.60 cap. A warning that fires on correct
    configs is worse than no warning: the operator learns to ignore it, and the
    real typo goes past just as silently as before.
    """
    catalog = [
        _o("a", "NVIDIA GeForce RTX 4090", 24, "12.8", 0.74),
        _o("b", "NVIDIA L4", 24, "12.8", 0.49),
    ]
    placement = Placement(
        min_vram_gb=24,
        max_usd_per_hr=0.60,
        accelerators=(
            "NVIDIA GeForce RTX 4090",
            "NVIDIA L4",
        ),
    )

    with caplog.at_level("WARNING"):
        kept = filter_offers(catalog, placement)

    assert [o.gpu_type for o in kept] == ["NVIDIA L4"]
    assert caplog.text == "", f"expected silence, got: {caplog.text!r}"


def test_no_warning_when_every_name_matches_the_catalog(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A correct cfg is silent.

    Bug caught: an implementation that warns unconditionally, or one that keys
    off "my first preference is not first in the result" — which is a routine,
    correct outcome whenever the preferred GPU is out of stock.
    """
    catalog = [_o("a", "NVIDIA L4", 24, "12.8", 0.49)]
    placement = Placement(
        min_vram_gb=24, max_usd_per_hr=10.0, accelerators=("NVIDIA L4",)
    )

    with caplog.at_level("WARNING"):
        filter_offers(catalog, placement)

    assert caplog.text == ""


def test_an_empty_catalog_does_not_warn_about_names(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty catalog is evidence about CAPACITY, not about names.

    Bug caught: comparing names against an empty ``available`` set makes every
    requested accelerator "unknown", so a provider whose catalog read returned
    nothing — every GPU null-priced, which RunPod does routinely — would accuse
    a perfectly correct cfg of naming GPUs that do not exist. The operator is
    then sent to fix a spelling that was never wrong while the real problem
    (no capacity) goes unmentioned.
    """
    placement = Placement(
        min_vram_gb=24, max_usd_per_hr=10.0, accelerators=("NVIDIA L4",)
    )

    with caplog.at_level("WARNING"):
        assert filter_offers([], placement) == []

    assert caplog.text == ""


def test_a_real_gpu_that_is_merely_out_of_stock_is_not_called_a_typo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Absence from the priced catalog is CAPACITY, not a spelling mistake.

    Bug caught, and caught live: ``find_offers`` drops null-priced GPUs before
    ``filter_offers`` ever sees them, so a GPU with no stock this minute looks
    exactly like a name that does not exist. On 2026-09-14 the first cut of this
    warning told the operator that ``NVIDIA RTX A5000`` — a real RunPod id,
    priced at $0.270 twenty minutes earlier — "matches nothing in this
    provider's catalog", and suggested renaming it to ``NVIDIA RTX A6000``.
    Advice to break a correct config is worse than silence.

    ``known_accelerators`` is the provider's FULL id set, including the
    unavailable ones, and it is the authority on whether a name exists.
    """
    priced = [_o("a", "NVIDIA L4", 24, "12.8", 0.49)]
    placement = Placement(
        min_vram_gb=24,
        max_usd_per_hr=10.0,
        accelerators=(
            "NVIDIA RTX A5000",
            "NVIDIA L4",
        ),
    )

    with caplog.at_level("WARNING"):
        filter_offers(
            priced,
            placement,
            known_accelerators={"NVIDIA RTX A5000", "NVIDIA L4", "NVIDIA A40"},
        )

    assert caplog.text == "", (
        "the A5000 is a real catalog id with no current stock; naming it a typo "
        f"is the cry-wolf failure this guard exists to avoid. Got: {caplog.text!r}"
    )


def test_the_suggestion_prefers_the_gpu_the_operator_actually_meant(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The hint must point at the same MODEL, not the nearest string.

    Bug caught, and caught live: plain ``difflib`` ranks ``NVIDIA RTX A4000``
    above ``NVIDIA GeForce RTX 4090`` as the closest match for
    ``NVIDIA RTX 4090``, because the shorter candidate scores better on raw
    character overlap. That hint sends the operator to a different GPU of a
    different generation — the warning is then not just unhelpful but wrong,
    and acting on it silently changes which hardware the cfg books.

    ``NVIDIA RTX 4090``'s tokens are a strict subset of
    ``NVIDIA GeForce RTX 4090``'s, which is the signal that separates "the same
    card, written loosely" from "a different card that looks similar".
    """
    priced = [
        _o("a", "NVIDIA GeForce RTX 4090", 24, "12.8", 0.74),
        _o("b", "NVIDIA RTX A4000", 16, "12.8", 0.30),
    ]
    placement = Placement(
        min_vram_gb=16, max_usd_per_hr=10.0, accelerators=("NVIDIA RTX 4090",)
    )

    with caplog.at_level("WARNING"):
        filter_offers(priced, placement)

    assert "did you mean 'NVIDIA GeForce RTX 4090'?" in caplog.text, (
        f"expected the 4090 suggestion, got: {caplog.text!r}"
    )
