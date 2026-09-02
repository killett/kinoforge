"""Pure offer-filtering helper applied by ComputeProvider.find_offers."""

from __future__ import annotations

from kinoforge.core.interfaces import Offer, Placement


def _cuda_tuple(v: str) -> tuple[int, ...]:
    """Parse a CUDA version string into a tuple of ints for semantic compare.

    Args:
        v: A CUDA version string such as ``"12.8"`` or ``"12.10"``.

    Returns:
        A tuple of ints, e.g. ``(12, 8)`` or ``(12, 10)``.
    """
    return tuple(int(p) for p in v.split("."))


def filter_offers(offers: list[Offer], placement: Placement) -> list[Offer]:
    """Return offers meeting *placement*, ranked by accelerator preference.

    compute-seam S4 folded ``HardwareRequirements`` into ``Placement``: the two
    described the same five numbers under different names, and the catalog
    filter's name implied every provider enumerates a catalog. Only the
    enumerating ones do, and they now call this themselves.

    The price ceiling stays a PRE-BOOK filter here, and that is deliberate.
    S4 also verifies the realized rate after launch, but on a provider with a
    catalog the filter is strictly better: it never books the over-cap instance
    in the first place, where the readback can only destroy one already
    running.

    Args:
        offers: Candidate offers from a provider's catalog.
        placement: The portable resource block to filter and rank by.

    Returns:
        Offers that pass all filters, sorted so that accelerators listed in
        ``placement.accelerators`` come first (in listed order); unlisted
        types come after, preserving the input order among themselves.
    """
    kept: list[Offer] = []
    for o in offers:
        if o.vram_gb < placement.min_vram_gb:
            continue
        if _cuda_tuple(o.cuda) < _cuda_tuple(placement.min_cuda):
            continue
        if o.mode == "pod" and o.cost_rate_usd_per_hr > placement.max_usd_per_hr:
            continue
        kept.append(o)

    if not placement.accelerators:
        return kept

    def rank(o: Offer) -> int:
        if o.gpu_type in placement.accelerators:
            return placement.accelerators.index(o.gpu_type)
        return len(placement.accelerators)

    return sorted(kept, key=rank)  # stable sort preserves input order within a rank
