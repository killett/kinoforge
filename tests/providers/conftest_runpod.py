"""RunPod test fixtures and recording HTTP seam (Layer N Task 1).

Provides three primitives used by the offline RunPod suite + live smoke:

- :func:`_load_fixture` — load a committed real-API JSON capture for
  replay-style offline tests.
- :class:`_RecordingHTTPSeam` — wrap real http_post / http_get callables, log
  every request, redact secrets, and dispatch responses to a fixed fixture
  filename via a pluggable dispatch callable.
- :func:`_redact` — recursively scrub any key whose name (whole-word, case
  insensitive) matches the protected vocab ``token / key / secret / password``.

Module-level dispatcher constants:

- :data:`_RUNPOD_DISPATCH` — GraphQL-query-based dispatcher (Layer N behaviour).
- :data:`_COMFY_DISPATCH` — URL-pattern-based dispatcher for ComfyUI traffic.
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


# Layer P Task 7 bug-fix #1 — Pass 1 (shape detector) vocab.
# RunPod's GraphQL env shape stores credentials as ``[{"key": NAME, "value":
# VAL}, ...]`` where NAME is the env-var identifier (e.g. ``RUNPOD_API_KEY``).
# ``_is_credential_name`` recognises NAMEs that look like credentials so the
# sibling ``value`` can be redacted.  This vocab is intentionally uppercase
# and suffix/whole-word oriented because env var names are conventionally
# uppercase snake_case (whereas ``_PROTECTED_WORDS`` above targets arbitrary
# dict key names in any casing).
_PROTECTED_NAME_SUFFIXES: frozenset[str] = frozenset(
    {"_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASSPHRASE"}
)
_PROTECTED_NAME_WHOLES: frozenset[str] = frozenset(
    {"KEY", "TOKEN", "SECRET", "PASSWORD", "PASSPHRASE"}
)


def _is_credential_name(name: str) -> bool:
    """Return True if *name* looks like a credential env var.

    Match policy: uppercase the input, then return True when it equals one of
    the bare whole-word forms (``KEY``, ``TOKEN``, ``SECRET``, ``PASSWORD``,
    ``PASSPHRASE``) OR ends with one of the suffix forms (``_KEY``,
    ``_TOKEN``, ``_SECRET``, ``_PASSWORD``, ``_PASSPHRASE``).

    Args:
        name: The env-var name to test (e.g. ``"RUNPOD_API_KEY"``).

    Returns:
        True for credential-shaped names like ``RUNPOD_API_KEY``, ``HF_TOKEN``,
        ``FAL_KEY``, ``DB_PASSWORD``, ``SSH_PASSPHRASE``.  False for
        non-credential names like ``IMAGE_NAME``, ``GPU_COUNT``,
        ``PYTHONUNBUFFERED``, or unrelated tokens like ``keypoints`` /
        ``checkpoints``.
    """
    upper = name.upper()
    if upper in _PROTECTED_NAME_WHOLES:
        return True
    return any(upper.endswith(suffix) for suffix in _PROTECTED_NAME_SUFFIXES)


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


def _redact_kv_shape(obj: Any) -> Any:
    """Recursively redact GraphQL ``[{"key": NAME, "value": VAL}, ...]`` env shapes.

    Pass 1 of the layered redactor (Layer P Task 7 bug-fix #1).  Walks every
    list; for each item that is a dict with both ``key`` AND ``value`` keys,
    checks whether the ``key`` field's STRING VALUE matches
    :func:`_is_credential_name`.  When it does, replaces the sibling ``value``
    field with ``<REDACTED>`` and recurses into the remaining sibling fields
    (e.g. ``comment``) so non-value siblings are preserved structurally but
    still scrubbed downstream.  Recurses into all other containers normally.

    Critically requires a LIST parent: a top-level ``{"key": ..., "value":
    ...}`` dict is NOT touched, because that shape isn't the GraphQL env
    array we're targeting.

    Args:
        obj: Any JSON-serialisable Python value.

    Returns:
        A redacted copy of ``obj``.  Original is not mutated.
    """
    if isinstance(obj, list):
        out_list: list[Any] = []
        for item in obj:
            if (
                isinstance(item, dict)
                and "key" in item
                and "value" in item
                and isinstance(item["key"], str)
                and _is_credential_name(item["key"])
            ):
                redacted_item: dict[str, Any] = dict(item)
                redacted_item["value"] = "<REDACTED>"
                for k, v in item.items():
                    if k != "value":
                        redacted_item[k] = _redact_kv_shape(v)
                out_list.append(redacted_item)
            else:
                out_list.append(_redact_kv_shape(item))
        return out_list
    if isinstance(obj, dict):
        return {k: _redact_kv_shape(v) for k, v in obj.items()}
    return obj


# Layer P Task 7 bug-fix #1 — Pass 3 (value-side credential-pattern sweep).
# Each entry is ``(pattern_name, compiled_regex)``.  The ``pattern_name`` is the
# canonical snake_case identifier used by Task 4 audit primitives and Task 5
# backstop tests; do not rename without coordinating those callers.
_CREDENTIAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("rpa_token", re.compile(r"\brpa_[A-Za-z0-9_\-]{8,}\b")),
    ("hf_token", re.compile(r"\bhf_[A-Za-z0-9_\-]{8,}\b")),
    ("fal_key", re.compile(r"\bfal_key_[A-Za-z0-9_\-]{8,}\b")),
    ("bearer_auth", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}")),
    ("sk_token", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "pem_private_key",
        re.compile(
            r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[\s\S]*?-----END [A-Z ]{0,40}PRIVATE KEY-----"
        ),
    ),
]


def _redact_string(s: str) -> str:
    """Apply every entry in :data:`_CREDENTIAL_PATTERNS` to *s* in declaration order.

    Each match is replaced with ``<REDACTED>``.  Multiple distinct patterns can
    fire within the same string (e.g. ``Bearer rpa_xxx`` triggers both
    ``bearer_auth`` and ``rpa_token`` — the first match wins for any given
    substring; later patterns operate on the partially-redacted output).

    Args:
        s: An arbitrary string value to scrub.

    Returns:
        ``s`` with every credential-shaped substring replaced by
        ``<REDACTED>``.  Non-matching strings pass through unchanged.
    """
    out = s
    for _name, pattern in _CREDENTIAL_PATTERNS:
        out = pattern.sub("<REDACTED>", out)
    return out


def _redact_credential_patterns(obj: Any) -> Any:
    """Pass 3 — recursive value-side credential-pattern sweep.

    Walks every nested container.  For each string value, applies
    :func:`_redact_string`.  Non-string scalars pass through unchanged.

    Args:
        obj: Any JSON-serialisable Python value (dict, list, str, int, etc).

    Returns:
        A redacted copy of ``obj``.  Original is not mutated.
    """
    if isinstance(obj, str):
        return _redact_string(obj)
    if isinstance(obj, dict):
        return {k: _redact_credential_patterns(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_credential_patterns(v) for v in obj]
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


# ---------------------------------------------------------------------------
# Dispatch callable type alias
# ---------------------------------------------------------------------------

DispatchFn = Callable[[str, "dict[str, Any] | None"], str]
"""Dispatch signature: (url, request_body_or_None) -> fixture_filename.

The wrapper passes the URL and (for POSTs) the request body to the
dispatcher, which returns a fixture filename.  Returning a name starting
with ``unknown_`` causes the wrapper to log a WARNING and still write the
capture.
"""


# ---------------------------------------------------------------------------
# RunPod dispatcher (GraphQL-query-based, Layer N behaviour)
# ---------------------------------------------------------------------------


def _runpod_dispatch(url: str, body: dict[str, Any] | None) -> str:
    """RunPod dispatcher — keys off GraphQL query content in POST body.

    Replicates the existing Layer N dispatch table verbatim.  The URL is
    ignored; RunPod posts everything to the same endpoint.

    Args:
        url: The request URL (ignored for RunPod).
        body: The POST body dict, or ``None`` for GET requests.

    Returns:
        A fixture filename such as ``create_pod.json`` or
        ``unknown_<sha8>.json``.
    """
    query = ""
    if body and isinstance(body.get("query"), str):
        query = body["query"]
    # For GET requests the URL may embed the query after ``?query=``; use it
    # as the fallback so existing GET dispatch still works.
    if not query and url and "?query=" in url:
        query = url.split("?query=", 1)[1]
    if "gpuTypes {" in query:
        return "gpu_types.json"
    if "myself { pods" in query:
        return "list_pods.json"
    if "pod(input:" in query:
        return "get_pod.json"
    if "podFindAndDeployOnDemand" in query:
        return "create_pod.json"
    if "podTerminate" in query:
        return "terminate_pod.json"
    sha = hashlib.sha256(query.encode()).hexdigest()[:8]
    _log.warning(
        "RecordingHTTPSeam: unrecognized GraphQL query, writing to "
        "unknown_%s.json (query fragment: %s)",
        sha,
        query[:80],
    )
    return f"unknown_{sha}.json"


_RUNPOD_DISPATCH: DispatchFn = _runpod_dispatch


# ---------------------------------------------------------------------------
# ComfyUI dispatcher (URL-pattern-based)
# ---------------------------------------------------------------------------

_COMFY_PROMPT_RE: re.Pattern[str] = re.compile(r"/prompt(\?.*)?$")
_COMFY_HISTORY_RE: re.Pattern[str] = re.compile(r"/history/[^/?]+(\?.*)?$")
_COMFY_VIEW_RE: re.Pattern[str] = re.compile(r"/view(\?|$)")


def _comfy_dispatch(url: str, body: dict[str, Any] | None) -> str:
    """ComfyUI dispatcher — keys off URL path.

    Args:
        url: The request URL.
        body: The POST body dict, or ``None`` for GET requests.

    Returns:
        A fixture filename such as ``prompt_submit.json`` or
        ``unknown_<sha8>.json``.
    """
    if _COMFY_PROMPT_RE.search(url):
        return "prompt_submit.json"
    if _COMFY_HISTORY_RE.search(url):
        return "history_done.json"
    if _COMFY_VIEW_RE.search(url):
        return "view.json"
    sha = hashlib.sha256(url.encode()).hexdigest()[:8]
    _log.warning(
        "RecordingHTTPSeam: unrecognized ComfyUI URL, writing to "
        "unknown_%s.json (url: %s)",
        sha,
        url[:120],
    )
    return f"unknown_{sha}.json"


_COMFY_DISPATCH: DispatchFn = _comfy_dispatch


# ---------------------------------------------------------------------------
# Recording seam
# ---------------------------------------------------------------------------


class _RecordingHTTPSeam:
    """Wrap real http_post / http_get callables for the live smoke.

    Each call is captured (request + redacted response + redacted request
    body).  At :meth:`flush`, one JSON file per logical operation is written
    to ``out_dir``; when multiple calls resolve to the same filename the last
    write wins.

    Args:
        http_post: Real POST callable to wrap.
        http_get: Real GET callable to wrap.
        out_dir: Output directory for written fixtures.
        dispatch: Callable ``(url, body_or_None) -> filename`` that maps each
            request to its fixture filename.  Use :data:`_RUNPOD_DISPATCH` for
            GraphQL-based RunPod traffic or :data:`_COMFY_DISPATCH` for
            URL-pattern-based ComfyUI traffic.
    """

    def __init__(
        self,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]],
        http_get: Callable[[str], dict[str, Any]],
        out_dir: Path,
        *,
        dispatch: DispatchFn = _RUNPOD_DISPATCH,
    ) -> None:
        self._post = http_post
        self._get = http_get
        self._out = out_dir
        self._dispatch_fn = dispatch
        # Each record: (filename, url, request_body_or_None, response)
        self._records: list[tuple[str, str, dict[str, Any] | None, dict[str, Any]]] = []

    def http_post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST wrapper — records the response under its operation name."""
        response = self._post(url, body)
        filename = self._dispatch_fn(url, body)
        self._records.append((filename, url, body, response))
        return response

    def http_get(self, url: str) -> dict[str, Any]:
        """GET wrapper — records the response under its operation name."""
        response = self._get(url)
        filename = self._dispatch_fn(url, None)
        self._records.append((filename, url, None, response))
        return response

    def flush(self) -> None:
        """Write one JSON file per recorded operation to ``out_dir``."""
        import subprocess

        try:
            git_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            git_sha = "UNKNOWN"

        self._out.mkdir(parents=True, exist_ok=True)
        for filename, url, request_body, response in self._records:
            # Redact both the request body and the response.
            redacted_body: dict[str, Any] | None = (
                _redact(request_body) if request_body is not None else None
            )
            # Build the _meta.request_query for backward-compat with Layer N
            # fixtures that carry the raw GraphQL query string.
            if request_body and isinstance(request_body.get("query"), str):
                raw_query = request_body["query"]
            elif "?query=" in url:
                raw_query = url.split("?query=", 1)[1]
            else:
                raw_query = url
            meta: dict[str, Any] = {
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "git_sha": git_sha,
                "operation": filename.removesuffix(".json"),
                "request_query": _redact_query_string(raw_query)[:200],
            }
            if redacted_body is not None:
                meta["request_body"] = redacted_body
            payload = {
                "_meta": meta,
                "response": _redact(response),
            }
            (self._out / filename).write_text(
                json.dumps(payload, indent=2, sort_keys=False) + "\n",
            )
