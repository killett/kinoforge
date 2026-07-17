"""Shared readiness-poll loop for pod-backed engines.

Consumed by the comfyui and diffusers engine subpackages, whose
``wait_for_ready`` methods were structural clones differing only in
the readiness endpoint (preferred port key + URL path). The engines
keep their public ``wait_for_ready`` methods (unchanged signatures,
engine-specific docstrings) and delegate the loop body here.

Every error message raised from this module is byte-identical to the
strings previously raised inside each engine — the messages never
mentioned the engine name, so no per-engine parameterization of the
strings is needed.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from kinoforge.core.boot_liveness import BootVerdict
from kinoforge.core.errors import ProvisionFailed, ProvisionTimeout

if TYPE_CHECKING:
    from collections.abc import Callable

    from kinoforge.core.boot_liveness import BootLivenessProbe
    from kinoforge.core.cancel import CancelToken
    from kinoforge.core.interfaces import Instance

#: Seconds to sleep between ready-check polls in :func:`poll_until_ready`.
READY_POLL_INTERVAL_S: float = 5.0

#: Throttle for the boot-liveness probe inside :func:`poll_until_ready` —
#: consulted at most this often, not on every readiness poll (2026-07-07).
BOOT_PROBE_INTERVAL_S: float = 30.0


def poll_until_ready(
    instance: Instance,
    *,
    http_get: Callable[[str], dict[str, Any]],
    sleep: Callable[[float], None],
    get_instance: Callable[[str], Instance],
    timeout_s: float,
    cancel_token: CancelToken | None,
    port_key: str,
    ready_path: str,
    boot_liveness_probe: BootLivenessProbe | None,
) -> None:
    """Poll an engine readiness URL until 200, terminal status, or timeout.

    Port-key heuristic: prefer ``port_key`` in ``instance.endpoints``,
    fall back to the first key present.

    Args:
        instance: The just-created compute instance.
        http_get: HTTP GET seam — raises on error, returns dict on success.
        sleep: Sleep seam used between polls.
        get_instance: Provider lookup for status checks between polls.
        timeout_s: Maximum total wait.
        cancel_token: C29 cooperative cancellation. Checked at the top of
            each poll iteration before any I/O. ``None`` preserves
            pre-C29 behaviour.
        port_key: Preferred key into ``instance.endpoints`` (e.g. ``"8188"``
            for ComfyUI, ``"8000"`` for the diffusers server).
        ready_path: URL path appended to the endpoint base (e.g.
            ``"/system_stats"`` or ``"/health"``).
        boot_liveness_probe: Optional provider-injected liveness probe
            (2026-07-07 boot-stall fast-fail). Consulted on its own
            throttle, not every readiness poll; GONE/STALLED abort in
            ~2-3min instead of waiting the full boot_timeout.

    Raises:
        ProvisionFailed: Pod entered terminal status before ready.
        ProvisionTimeout: ``timeout_s`` elapsed without a successful ready check.
        Cancelled: ``cancel_token`` was set during the wait.
    """
    if not instance.endpoints:
        raise ProvisionFailed(
            f"pod {instance.id!r} has no endpoints — cannot construct ready URL"
        )
    key = (
        port_key
        if port_key in instance.endpoints
        else next(iter(instance.endpoints), port_key)
    )
    base = instance.endpoints.get(key, "")
    ready_url = f"{base.rstrip('/')}{ready_path}"

    start = time.monotonic()
    last_probe = start - BOOT_PROBE_INTERVAL_S  # allow a probe on first idle poll
    while True:
        if cancel_token is not None:
            cancel_token.raise_if_set()
        now = time.monotonic()
        if now - start >= timeout_s:
            raise ProvisionTimeout(
                f"engine ready check timed out after {timeout_s:.0f}s "
                f"for pod {instance.id!r}"
            )
        try:
            http_get(ready_url)
            return
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            current = get_instance(instance.id)
        except KeyError as exc:
            raise ProvisionFailed(
                f"pod {instance.id!r} vanished during boot (provider "
                f"no longer knows it)"
            ) from exc
        if current.status in ("terminated", "stopped"):
            raise ProvisionFailed(
                f"pod {instance.id!r} entered terminal status "
                f"{current.status!r} before ready"
            )
        # 2026-07-07 boot-stall fast-fail: consult the injected liveness
        # probe on its own throttle (not every readiness poll). GONE/STALLED
        # abort in ~2-3min instead of waiting the full boot_timeout.
        if (
            boot_liveness_probe is not None
            and now - last_probe >= BOOT_PROBE_INTERVAL_S
        ):
            last_probe = now
            verdict = boot_liveness_probe.check(instance.id)
            if verdict is BootVerdict.GONE:
                raise ProvisionFailed(f"pod {instance.id!r} vanished during boot")
            if verdict is BootVerdict.STALLED:
                raise ProvisionFailed(
                    f"pod {instance.id!r} boot stalled (provision crashed "
                    f"or util flatline) — aborting before boot_timeout"
                )
        sleep(READY_POLL_INTERVAL_S)
