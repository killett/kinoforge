"""Pure offer-filtering helper applied by ComputeProvider.find_offers."""

from __future__ import annotations

from kinoforge.core.interfaces import HardwareRequirements, Offer


def _cuda_tuple(v: str) -> tuple[int, ...]:
    """Parse a CUDA version string into a tuple of ints for semantic compare.

    Args:
        v: A CUDA version string such as ``"12.8"`` or ``"12.10"``.

    Returns:
        A tuple of ints, e.g. ``(12, 8)`` or ``(12, 10)``.
    """
    return tuple(int(p) for p in v.split("."))


def filter_offers(offers: list[Offer], reqs: HardwareRequirements) -> list[Offer]:
    """Return offers meeting reqs, ordered by gpu_preference then input order.

    Args:
        offers: Candidate offers from a provider's offer source.
        reqs: Hardware filter to apply.

    Returns:
        Offers that pass all filters, sorted so that GPU types listed in
        ``reqs.gpu_preference`` come first (in listed order); unlisted GPU
        types come after, preserving the input order among themselves.
    """
    kept: list[Offer] = []
    for o in offers:
        if o.vram_gb < reqs.min_vram_gb:
            continue
        if _cuda_tuple(o.cuda) < _cuda_tuple(reqs.min_cuda):
            continue
        if o.mode == "pod" and o.cost_rate_usd_per_hr > reqs.max_cost_rate_usd_per_hr:
            continue
        kept.append(o)

    if not reqs.gpu_preference:
        return kept

    def rank(o: Offer) -> int:
        if o.gpu_type in reqs.gpu_preference:
            return reqs.gpu_preference.index(o.gpu_type)
        return len(reqs.gpu_preference)

    return sorted(kept, key=rank)  # stable sort preserves input order within a rank
