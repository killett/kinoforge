"""Pure offer-filtering helper applied by ComputeProvider.find_offers."""

from __future__ import annotations

import difflib
import logging
from collections.abc import Sequence

from kinoforge.core.interfaces import Offer, Placement

_LOG = logging.getLogger(__name__)


def _cuda_tuple(v: str) -> tuple[int, ...]:
    """Parse a CUDA version string into a tuple of ints for semantic compare.

    Args:
        v: A CUDA version string such as ``"12.8"`` or ``"12.10"``.

    Returns:
        A tuple of ints, e.g. ``(12, 8)`` or ``(12, 10)``.
    """
    return tuple(int(p) for p in v.split("."))


def filter_offers(
    offers: list[Offer],
    placement: Placement,
    known_accelerators: set[str] | None = None,
) -> list[Offer]:
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
        known_accelerators: Every accelerator id the provider's catalog carries,
            INCLUDING ones with no current stock. Used only to tell a misspelled
            accelerator name from a real one that is temporarily unavailable
            (U43). ``None`` falls back to the names present in ``offers``, which
            is weaker evidence — a stocked-out GPU then reads as unknown.

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

    _warn_unknown_accelerators(offers, placement.accelerators, known_accelerators)

    def rank(o: Offer) -> int:
        if o.gpu_type in placement.accelerators:
            return placement.accelerators.index(o.gpu_type)
        return len(placement.accelerators)

    return sorted(kept, key=rank)  # stable sort preserves input order within a rank


def _tokens(name: str) -> frozenset[str]:
    """Return *name*'s lowercased word set, for same-model comparison.

    Args:
        name: An accelerator id or the operator's spelling of one.

    Returns:
        The set of lowercased whitespace-separated tokens.
    """
    return frozenset(name.lower().split())


def _suggest(name: str, available: set[str]) -> str | None:
    """Return the catalog id *name* most likely meant, or ``None``.

    Prefers a candidate whose tokens are a SUPERSET of the requested ones —
    ``NVIDIA RTX 4090`` against ``NVIDIA GeForce RTX 4090`` is the same card
    written loosely, and that relationship survives the extra word where raw
    character overlap does not. Plain ``difflib`` ranks the shorter
    ``NVIDIA RTX A4000`` higher, which would send the operator to a different
    GPU of a different generation; measured 2026-09-14 against the live catalog.

    Falls back to ``difflib`` when no candidate is a token superset, since a
    fuzzy hint still beats none for a genuine misspelling.

    Args:
        name: The unmatched accelerator name the operator wrote.
        available: Every accelerator id the provider's catalog carries.

    Returns:
        The suggested id, or ``None`` when nothing is close enough.
    """
    wanted = _tokens(name)
    supersets = [c for c in sorted(available) if wanted < _tokens(c)]
    if supersets:
        # Shortest superset = fewest unexplained extra words.
        return min(supersets, key=lambda c: (len(_tokens(c)), c))
    close = difflib.get_close_matches(name, sorted(available), n=1)
    return close[0] if close else None


def _warn_unknown_accelerators(
    catalog: list[Offer],
    requested: Sequence[str],
    known_accelerators: set[str] | None = None,
) -> None:
    """Warn for requested accelerator names that appear nowhere in *catalog*.

    U43. :func:`filter_offers` ranks by exact string equality, so a name the
    provider's catalog does not carry scores the same as a GPU nobody asked for
    — the preference is INERT, and a cfg with a typo is indistinguishable from
    one that works. That is not hypothetical: seven shipped cfgs name GPUs
    RunPod does not have (``NVIDIA RTX 4090`` where the catalog id is
    ``NVIDIA GeForce RTX 4090``, ``A100 80GB``, ``H100 80GB``, ``A100 40GB``),
    and on the 1.3B grid cfg that left an L4 leading the offer list ahead of
    the 4090 the cfg meant to prefer — the whole of U36's nine-attempt
    create-then-destroy loop.

    Three things this deliberately does NOT do. It compares against the FULL
    catalog, not the filtered result, so a named GPU that is merely over cap or
    undersized stays silent — that is correct behaviour and warning on it would
    train the operator to ignore the warning. It says nothing when the catalog
    is empty, because then there is no evidence about names at all, only about
    capacity. And where the provider supplies ``known_accelerators`` it judges
    against THAT, not against what happens to be in stock: on 2026-09-14 the
    first cut of this warning told the operator that ``NVIDIA RTX A5000`` — a
    real id, priced at $0.270 twenty minutes earlier — matched nothing, because
    ``find_offers`` drops null-priced GPUs before this sees them. Advice to
    break a working config is worse than silence.

    Args:
        catalog: Every offer the provider enumerated, BEFORE filtering.
        requested: The accelerator names the operator wrote, in cfg order.
        known_accelerators: The provider's full id set including out-of-stock
            entries. ``None`` falls back to the names present in ``catalog``.
    """
    available = (
        known_accelerators
        if known_accelerators is not None
        else {o.gpu_type for o in catalog}
    )
    if not available:
        return
    for name in requested:
        if name in available:
            continue
        suggestion = _suggest(name, available)
        hint = f"; did you mean {suggestion!r}?" if suggestion else ""
        _LOG.warning(
            "[placement] accelerator %r matches nothing in this provider's "
            "catalog, so it ranks no higher than a GPU you did not ask for%s",
            name,
            hint,
        )
