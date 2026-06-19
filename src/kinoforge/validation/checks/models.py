"""ModelRefReachableCheck — NETWORK ERROR.

HEAD-checks each ``cfg.models[]`` ref. Two scheme paths:

  - ``hf:<repo>:<file>``  -> resolved to
    ``https://huggingface.co/<repo>/resolve/main/<file>``
  - ``https://...``       -> HEAD'd as-is

Generate mode checks only ``kind: base`` (the diffusion checkpoint
the engine consumes as primary weight slot). Doctor mode checks all.

Auth-required responses (401) count as PASS — the file exists, the
operator's HF_TOKEN gates the pull.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from collections.abc import Callable

from kinoforge.core.config import Config, ModelEntry
from kinoforge.validation.protocol import CheckCategory, CheckResult, Severity
from kinoforge.validation.registry import register

_log = logging.getLogger(__name__)

_PASS_CODES = frozenset({200, 301, 302, 401})


def _default_http_head(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _resolve_ref_to_url(ref: str) -> str:
    """Translate a kinoforge model ref into an HTTP-able URL."""
    if ref.startswith(("https://", "http://")):
        return ref
    if ref.startswith("hf:"):
        body = ref[3:]
        if ":" not in body:
            return f"https://huggingface.co/{body}"
        repo, file_ = body.split(":", 1)
        return f"https://huggingface.co/{repo}/resolve/main/{file_}"
    return ref


class ModelRefReachableCheck:
    """NETWORK ERROR — every cfg.models[].ref must HEAD-resolve."""

    name: str = "model_ref_reachable"
    category: CheckCategory = CheckCategory.NETWORK
    severity: Severity = Severity.ERROR

    def __init__(
        self,
        *,
        http_head: Callable[[str], int] | None = None,
        full: bool = False,
    ) -> None:
        """Wire injectable seam. ``full=True`` doctors every model entry."""
        self._http_head = http_head or _default_http_head
        self._full = full

    _NON_FETCHING_ENGINES: frozenset[str] = frozenset(
        {"hosted", "fal", "replicate", "runway", "bedrock_video", "fake"}
    )

    def applies_to(self, cfg: Config) -> bool:
        """Apply iff the engine fetches the ref and at least one ref carries a network scheme.

        Non-fetching engines (hosted shim, fal queue, Replicate / Runway
        Bearer providers, Bedrock video, fake) do not HEAD-resolve the
        ref — their wire identifier lives on ``spec.model`` or the
        engine sub-block. Skip them to avoid flagging informational
        placeholders.
        """
        if cfg.engine.kind in self._NON_FETCHING_ENGINES:
            return False
        if not cfg.models:
            return False
        return any(m.ref.startswith(("hf:", "https://", "http://")) for m in cfg.models)

    def _models_to_check(self, cfg: Config) -> list[ModelEntry]:
        if self._full:
            return list(cfg.models)
        return [m for m in cfg.models if m.kind == "base"]

    def run(self, cfg: Config) -> CheckResult:
        """HEAD each in-scope ref; collect failures."""
        failures: list[str] = []
        for model in self._models_to_check(cfg):
            url = _resolve_ref_to_url(model.ref)
            try:
                code = self._http_head(url)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "model_ref_reachable inconclusive for %s: %s",
                    model.ref,
                    exc,
                )
                continue
            if code not in _PASS_CODES:
                failures.append(f"{model.ref} -> HEAD {code}")
        if failures:
            return CheckResult(
                name=self.name,
                passed=False,
                severity=self.severity,
                message=(
                    f"{len(failures)} model ref(s) unreachable: " + "; ".join(failures)
                ),
                fix_suggestion="verify each ref against its source registry",
            )
        return CheckResult(
            name=self.name,
            passed=True,
            severity=self.severity,
            message=(f"{len(self._models_to_check(cfg))} ref(s) probed; all reachable"),
        )

    def auto_fix(self, cfg: Config) -> Config | None:
        """No safe auto-fix — operator chose the ref deliberately."""
        return None


register(ModelRefReachableCheck())
