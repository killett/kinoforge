"""Static Modal GPU offer catalog.

Modal is serverless — there is no live "offers" API. Pricing is a fixed table
(snapshot 2026-09-17, https://modal.com/pricing). Offers are ``mode="serverless"``
so :func:`filter_offers` does not apply the pod ``max_usd_per_hr`` cap.
"""

from __future__ import annotations

from kinoforge.core.interfaces import Offer, Placement
from kinoforge.core.offers import filter_offers

#: Modal GPU catalog: (Modal gpu-string, VRAM GB, $/hr snapshot).
_MODAL_GPUS: tuple[tuple[str, int, float], ...] = (
    ("T4", 16, 0.59),
    ("L4", 24, 0.80),
    ("A10", 24, 1.10),
    ("L40S", 48, 1.95),
    ("A100-40GB", 40, 2.10),
    ("A100-80GB", 80, 2.50),
    ("H100", 80, 3.95),
    # H200 is the only card here above 80 GB, and it is deliberately the only
    # one. Modal's GPU docs state its capacity verbatim ("141 GB"); they do NOT
    # state B200's or B300's, and a vram_gb that the provider never published is
    # the U48 defect — a fabricated constant that filter_offers applies
    # silently, with no error to read. Add B200/B300 only when Modal itself
    # publishes their VRAM.
    ("H200", 141, 4.54),
)

#: Modal's GPU fleet runs recent NVIDIA drivers (CUDA 12.8+). Report 12.8 so the
#: catalog survives the default ``Placement.min_cuda`` ("12.8"); a
#: lower "conservative" baseline would make ``filter_offers`` drop every offer.
_MODAL_CUDA = "12.8"

MODAL_GPU_CATALOG: tuple[Offer, ...] = tuple(
    Offer(
        id=name,
        gpu_type=name,
        vram_gb=vram,
        cuda=_MODAL_CUDA,
        cost_rate_usd_per_hr=cost,
        mode="serverless",
    )
    for name, vram, cost in _MODAL_GPUS
)


def modal_offers(placement: Placement) -> list[Offer]:
    """Return catalog offers filtered/ordered per ``reqs``.

    Args:
        placement: The portable resource block from the resolved config.

    Returns:
        Offers meeting ``min_vram_gb``/``min_cuda``, ordered by ``accelerators``.
    """
    return filter_offers(list(MODAL_GPU_CATALOG), placement)
