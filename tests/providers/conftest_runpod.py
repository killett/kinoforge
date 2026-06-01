"""RunPod test fixtures and recording HTTP seam (Layer N Task 1).

Provides three primitives used by the offline RunPod suite + live smoke:

- :func:`_load_fixture` — load a committed real-API JSON capture for
  replay-style offline tests.
- :class:`_RecordingHTTPSeam` — wrap real http_post / http_get callables, log
  every request, redact secrets, and dispatch responses to a fixed fixture
  filename via a query-fragment table.
- :func:`_redact` — recursively scrub any key whose name (whole-word, case
  insensitive) matches the protected vocab ``token / key / secret / password``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_FIXTURE_DIR: Path = Path(__file__).parent / "fixtures" / "runpod"

# Splits a camelCase / snake_case / kebab-case identifier into lower-cased
# segments so that "apiKey" → ["api", "key"] and "checkpoint" → ["checkpoint"].
_WORD_SPLIT_RE: re.Pattern[str] = re.compile(
    r"[_\-]|(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
)

_REDACT_QUERY_PARAM_RE: re.Pattern[str] = re.compile(
    r"(?i)([?&](?:[a-z_-]*(?:token|key|secret|password)[a-z_-]*)=)([^&\s]+)",
)


def _redact_query_string(s: str) -> str:
    """Scrub `?token=…` / `&api_key=…` style credentials from a query string."""
    return _REDACT_QUERY_PARAM_RE.sub(r"\1<REDACTED>", s)


_OPERATION_TABLE: list[tuple[str, str]] = [
    ("gpuTypes {", "gpu_types.json"),
    ("myself { pods", "list_pods.json"),
    ("podFindAndDeployOnDemand", "create_pod.json"),
    ("podTerminate", "terminate_pod.json"),
    ("pod(input:", "get_pod.json"),
]

_log = logging.getLogger(__name__)


_PROTECTED_WORDS: frozenset[str] = frozenset({"token", "key", "secret", "password"})


def _is_protected_key(name: str) -> bool:
    """Return True if any camelCase/snake_case/kebab-case segment of *name* is a protected word.

    Args:
        name: A dict key string to test.

    Returns:
        True when a whole segment exactly matches token, key, secret, or password
        (case-insensitive).  Partial matches like ``checkpoint`` → False.
    """
    segments = _WORD_SPLIT_RE.split(name)
    return any(seg.lower() in _PROTECTED_WORDS for seg in segments if seg)


def _redact(obj: Any) -> Any:
    """Recursively replace values at protected key names with ``<REDACTED>``.

    The match is case-insensitive and whole-word (or hyphen/underscore-bounded)
    against the vocab ``token, key, secret, password``.  Substrings such as
    ``checkpoint`` or ``keypoints`` pass through untouched.

    Args:
        obj: Any JSON-serialisable Python value (dict, list, str, int, etc).

    Returns:
        A redacted copy of ``obj``.  Original is not mutated.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and _is_protected_key(k):
                out[k] = "<REDACTED>"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _load_fixture(name: str) -> dict[str, Any]:
    """Load the ``response`` payload of a committed real-API capture.

    Args:
        name: File name relative to ``tests/providers/fixtures/runpod/``.

    Returns:
        The contents of the ``response`` block as a ``dict``.

    Raises:
        FileNotFoundError: The fixture does not exist; the message includes a
            copy-pasteable command for regenerating fixtures via the live
            smoke.
    """
    path = _FIXTURE_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"RunPod fixture not found: {path}.  Regenerate with:\n"
            f"  KINOFORGE_LIVE_TESTS=1 KINOFORGE_SAVE_FIXTURES=1 "
            f"pixi run pytest tests/live/test_runpod_live.py -v",
        )
    with path.open() as f:
        data = json.load(f)
    return dict(data["response"])


class _RecordingHTTPSeam:
    """Wrap real http_post / http_get callables for the live smoke.

    Each call is captured (request + redacted response).  At :meth:`flush`,
    one JSON file per logical operation is written to ``out_dir``.

    Args:
        http_post: Real POST callable to wrap.
        http_get: Real GET callable to wrap.
        out_dir: Output directory for written fixtures.
    """

    def __init__(
        self,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]],
        http_get: Callable[[str], dict[str, Any]],
        out_dir: Path,
    ) -> None:
        self._post = http_post
        self._get = http_get
        self._out = out_dir
        self._records: list[tuple[str, str, dict[str, Any]]] = []

    def http_post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST wrapper — records the response under its operation name."""
        response = self._post(url, body)
        query = str(body.get("query", ""))
        filename = self._dispatch(query)
        self._records.append((filename, query, response))
        return response

    def http_get(self, url: str) -> dict[str, Any]:
        """GET wrapper — records the response under its operation name."""
        response = self._get(url)
        query = url.split("?query=", 1)[1] if "?query=" in url else url
        filename = self._dispatch(query)
        self._records.append((filename, query, response))
        return response

    def flush(self) -> None:
        """Write one JSON file per recorded operation to ``out_dir``."""
        self._out.mkdir(parents=True, exist_ok=True)
        for filename, query, response in self._records:
            payload = {
                "_meta": {
                    "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "operation": filename.removesuffix(".json"),
                    "request_query": _redact_query_string(query)[:200],
                },
                "response": _redact(response),
            }
            (self._out / filename).write_text(
                json.dumps(payload, indent=2, sort_keys=False) + "\n",
            )

    def _dispatch(self, query: str) -> str:
        """Map a GraphQL query string to its fixture filename."""
        for fragment, filename in _OPERATION_TABLE:
            if fragment in query:
                return filename
        sha8 = hashlib.sha256(query.encode("utf-8")).hexdigest()[:8]
        _log.warning(
            "RecordingHTTPSeam: unrecognized GraphQL query, writing to "
            "unknown_%s.json (query fragment: %s)",
            sha8,
            query[:80],
        )
        return f"unknown_{sha8}.json"
