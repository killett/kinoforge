"""Static Modal GPU offer catalog.

Modal is serverless — there is no live "offers" API. Pricing is a fixed table
(snapshot 2026-07-08, https://modal.com/pricing). Offers are ``mode="serverless"``
so :func:`filter_offers` does not apply the pod ``max_usd_per_hr`` cap.
"""

from __future__ import annotations

from kinoforge.core.interfaces import HardwareRequirements, Offer
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
)

#: Modal's GPU fleet runs recent NVIDIA drivers (CUDA 12.8+). Report 12.8 so the
#: catalog survives the default ``HardwareRequirements.min_cuda`` ("12.8"); a
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


def modal_offers(reqs: HardwareRequirements) -> list[Offer]:
    """Return catalog offers filtered/ordered per ``reqs``.

    Args:
        reqs: Hardware requirements from the resolved config.

    Returns:
        Offers meeting ``min_vram_gb``/``min_cuda``, ordered by ``gpu_preference``.
    """
    return filter_offers(list(MODAL_GPU_CATALOG), reqs)
