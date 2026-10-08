"""The concrete RunPod-proxy ``/health`` probe the CLI injects into core.

``kinoforge.core.upscale_dir`` (and any future directory-scale runner) takes
a ``health_probe`` seam but must never import an adapter namespace itself —
``kinoforge.core`` is adapter-free by invariant (``tests/test_core_invariant.py::
test_no_adapter_imports_in_core``). This module is the CLI-side concrete probe:
it owns the ``kinoforge.engines._pod_http`` / ``kinoforge.engines._proxy_retry``
imports so core never has to.
"""

from __future__ import annotations

import time
from typing import Any

from kinoforge.core.cancel import CancelToken
from kinoforge.engines._pod_http import http_json
from kinoforge.engines._proxy_retry import retry_proxy_call

_HEALTH_USER_AGENT = "kinoforge-upscale-dir/0.1"


def probe_pod_health(url: str, cancel_token: CancelToken | None) -> dict[str, Any]:
    """GET ``/health`` with bounded retry on the RunPod proxy's transient 404/502/503/504.

    A dead pod and a routine proxy warmup blip look identical on a single
    request (``kinoforge/engines/_pod_http.py`` documents the same proxy
    tolerance every other engine call goes through). Routing the probe
    through :func:`~kinoforge.engines._proxy_retry.retry_proxy_call` means
    one transient hiccup does not get misread as a dead pod and abort a
    whole directory run.

    Args:
        url: The pod's ``/health`` endpoint (proxy URL).
        cancel_token: Cooperative-cancellation token honored across the
            retry's backoff waits.

    Returns:
        Decoded JSON response from the pod's health endpoint.
    """
    return retry_proxy_call(
        label="upscale_dir.health",
        url=url,
        fn=lambda: http_json(
            method="GET", url=url, payload=None, user_agent=_HEALTH_USER_AGENT
        ),
        sleep=time.sleep,
        cancel_token=cancel_token,
    )
