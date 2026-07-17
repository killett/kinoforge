"""Shared stdlib HEAD probe + pass-code policy for validation checks.

One decision — the redirect/timeout policy of reachability HEADs and
which statuses count as "resource exists" — previously copy-pasted
across models / image / custom_nodes.
"""

from __future__ import annotations

import urllib.error
import urllib.request

# "Resource exists" statuses for registries that may auth-gate HEADs
# (Hugging Face, Docker registries): a 401 proves the path resolves even
# though anonymous HEAD is refused. custom_nodes deliberately does NOT
# use this set — a 401 on a public GitHub commit URL means a bad ref.
PASS_CODES_AUTH_OK = frozenset({200, 301, 302, 401})


def default_http_head(url: str) -> int:
    """HEAD ``url`` and return the HTTP status code.

    Args:
        url: The URL to probe.

    Returns:
        The response status, or the error status for HTTP-level failures
        (404 etc.) so callers can classify; transport errors propagate.
    """
    req = urllib.request.Request(url, method="HEAD")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
