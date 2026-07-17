"""ImageReachableCheck — NETWORK ERROR.

HEAD-checks the docker image referenced by ``cfg.compute.image``
against the Docker Hub v2 registry. Catches placeholder image names
that ship in example cfgs but never resolve on a real pull (the
``skypilot/skypilot-gpu:latest`` case from the 2026-06-18 Stage E
smoke).

Auth-required responses (401) count as PASS — the image exists, the
registry is just protecting the pull. The pull will succeed once
the operator is logged in.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from kinoforge.core.config import Config
from kinoforge.validation.checks._head import PASS_CODES_AUTH_OK, default_http_head
from kinoforge.validation.protocol import CheckCategory, CheckResult, Severity
from kinoforge.validation.registry import register

_log = logging.getLogger(__name__)

_PASS_CODES = PASS_CODES_AUTH_OK

_DOCKER_HUB_HEAD_URL = "https://registry-1.docker.io/v2/{image}/manifests/{tag}"


def _parse_image_ref(image: str) -> tuple[str, str]:
    """Split ``namespace/name:tag`` into ``(image, tag)``.

    Tag defaults to ``latest``. Bare names get the implicit
    ``library/`` prefix per Docker Hub's convention.
    """
    if ":" in image:
        ref, tag = image.rsplit(":", 1)
    else:
        ref, tag = image, "latest"
    if "/" not in ref:
        ref = f"library/{ref}"
    return ref, tag


_default_http_head = default_http_head


class ImageReachableCheck:
    """NETWORK ERROR — compute.image must HEAD-resolve on its registry."""

    name: str = "image_reachable"
    category: CheckCategory = CheckCategory.NETWORK
    severity: Severity = Severity.ERROR

    def __init__(self, *, http_head: Callable[[str], int] | None = None) -> None:
        """Wire an injectable HEAD seam (defaults to stdlib urllib)."""
        self._http_head = http_head or _default_http_head

    def applies_to(self, cfg: Config) -> bool:
        """Apply iff compute.image is non-empty."""
        return cfg.compute is not None and bool(cfg.compute.image)

    def run(self, cfg: Config) -> CheckResult:
        """HEAD the Docker Hub v2 manifest endpoint for compute.image."""
        assert cfg.compute is not None  # noqa: S101 — guarded by applies_to
        image = cfg.compute.image
        ref, tag = _parse_image_ref(image)
        url = _DOCKER_HUB_HEAD_URL.format(image=ref, tag=tag)
        try:
            code = self._http_head(url)
        except Exception as exc:  # noqa: BLE001 — flaky upstream must not block
            _log.warning("image_reachable inconclusive for %s: %s", image, exc)
            return CheckResult(
                name=self.name,
                passed=True,
                severity=Severity.WARN,
                message=(
                    f"network probe inconclusive for {image}: {exc}; not blocking"
                ),
            )
        if code in _PASS_CODES:
            return CheckResult(
                name=self.name,
                passed=True,
                severity=self.severity,
                message=f"HEAD {code} for {image}",
            )
        return CheckResult(
            name=self.name,
            passed=False,
            severity=self.severity,
            message=(
                f"image {image} returned HEAD {code} from Docker Hub; "
                "the tag does not exist on the registry"
            ),
            fix_suggestion=(
                "pick a real published image (verify via "
                f"https://hub.docker.com/v2/repositories/{ref}/tags )"
            ),
        )

    def auto_fix(self, cfg: Config) -> Config | None:
        """No safe auto-fix — operator chose the image deliberately."""
        return None


register(ImageReachableCheck())
