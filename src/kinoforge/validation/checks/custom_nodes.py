"""CustomNodeSHAReachableCheck — NETWORK WARN.

HEAD-checks each ComfyUI custom-node ref against GitHub. WARN-only
because archived commits may still be cached on the pod from a prior
boot; the operator should know about the staleness but not be
blocked.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from collections.abc import Callable

from kinoforge.core.config import Config
from kinoforge.validation.protocol import CheckCategory, CheckResult, Severity
from kinoforge.validation.registry import register

_log = logging.getLogger(__name__)

_PASS_CODES = frozenset({200, 301, 302})


def _default_http_head(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _commit_url(git_url: str, ref: str) -> str:
    """Build the GitHub commit URL for a custom-node entry.

    ``git_url`` example: ``https://github.com/kijai/ComfyUI-KJNodes(.git)?``
    """
    base = git_url.rstrip("/")
    if base.endswith(".git"):
        base = base[:-4]
    return f"{base}/commit/{ref}"


class CustomNodeSHAReachableCheck:
    """NETWORK WARN — each ComfyUI custom-node SHA must HEAD on GitHub."""

    name: str = "custom_node_sha_reachable"
    category: CheckCategory = CheckCategory.NETWORK
    severity: Severity = Severity.WARN

    def __init__(self, *, http_head: Callable[[str], int] | None = None) -> None:
        """Wire an injectable HEAD seam."""
        self._http_head = http_head or _default_http_head

    def applies_to(self, cfg: Config) -> bool:
        """Apply iff engine is ComfyUI and custom_nodes is non-empty."""
        if cfg.engine is None or cfg.engine.kind != "comfyui":
            return False
        comfyui = cfg.engine.comfyui
        if comfyui is None:
            return False
        return bool(comfyui.custom_nodes)

    def run(self, cfg: Config) -> CheckResult:
        """HEAD each custom-node commit URL."""
        assert cfg.engine is not None  # noqa: S101 — guarded by applies_to
        assert cfg.engine.comfyui is not None  # noqa: S101 — guarded by applies_to
        nodes = cfg.engine.comfyui.custom_nodes
        misses: list[str] = []
        for node in nodes:
            git = str(node.get("git", ""))
            ref = str(node.get("ref", ""))
            if not git or not ref:
                continue
            url = _commit_url(git, ref)
            try:
                code = self._http_head(url)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "custom_node_sha_reachable inconclusive for %s@%s: %s",
                    git,
                    ref,
                    exc,
                )
                continue
            if code not in _PASS_CODES:
                misses.append(f"{git}@{ref} (HEAD {code})")
        if misses:
            return CheckResult(
                name=self.name,
                passed=False,
                severity=self.severity,
                message=(
                    f"{len(misses)} custom-node SHA(s) not reachable on "
                    f"GitHub: " + "; ".join(misses)
                ),
                fix_suggestion=(
                    "pin a current commit, or accept that the pod's "
                    "cached install may not match origin"
                ),
            )
        return CheckResult(
            name=self.name,
            passed=True,
            severity=self.severity,
            message=f"{len(nodes)} custom-node SHA(s) reachable",
        )

    def auto_fix(self, cfg: Config) -> Config | None:
        """No safe auto-fix — pinning a SHA is the operator's call."""
        return None


register(CustomNodeSHAReachableCheck())
